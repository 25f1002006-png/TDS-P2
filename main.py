import os
import json
import asyncio
import requests
import traceback
import time
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel
from playwright.async_api import async_playwright
from google import genai
from google.genai import types

# --- Configuration ---
app = FastAPI()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") 
client = genai.Client(api_key=GEMINI_API_KEY)

# Use the model version confirmed in your logs
MODEL_NAME = "gemini-2.0-flash-001" 

# --- Startup Check ---
@app.on_event("startup")
async def startup_event():
    print(f"--- Starting Quiz Bot with Model: {MODEL_NAME} ---")
    try:
        # Quick verify that we can list models
        models = [m.name for m in client.models.list()]
        print(f"Available Models (first 5): {models[:5]}")
    except Exception as e:
        print(f"Startup Warning: Could not list models: {e}")

# --- Data Models ---
class QuizRequest(BaseModel):
    email: str
    secret: str
    url: str

# --- Helper: Robust JSON Parser ---
def parse_json_response(text: str):
    """
    Cleans markdown (```json ... ```) and handles List vs Dict responses.
    """
    try:
        clean_text = text.strip()
        # Remove markdown code blocks if present
        if clean_text.startswith("```"):
            lines = clean_text.split("\n")
            # Remove first line (```json) and last line (```)
            clean_text = "\n".join(lines[1:-1])
        
        data = json.loads(clean_text)
        
        # FIX: If Gemini returns a list [{...}], return the first item
        if isinstance(data, list):
            if len(data) > 0:
                return data[0]
            else:
                return {}
        return data
    except Exception as e:
        print(f"JSON Parse Error: {e}")
        print(f"Raw Text was: {text}")
        return None

# --- Helper: Code Execution ---
def execute_generated_code(code_str: str):
    """
    Executes LLM-generated code safely.
    """
    local_scope = {}
    try:
        # Common imports available to the LLM
        import pandas as pd
        import numpy as np
        import json
        import requests
        import io
        import PyPDF2
        import re
        
        # Remove markdown wrapping for code too
        code_str = code_str.strip()
        if code_str.startswith("```"):
            code_str = "\n".join(code_str.split("\n")[1:-1])

        print("--- Executing Code ---")
        # print(code_str) # Uncomment to debug generated code
        
        exec(code_str, globals(), local_scope)
        
        if "get_answer" in local_scope:
            return local_scope["get_answer"]()
        else:
            print("Error: Generated code did not define get_answer()")
            return None
    except Exception as e:
        print(f"Code Execution Failed: {e}")
        traceback.print_exc()
        return None

# --- Core Logic ---
async def solve_quiz_recursive(start_url: str, email: str, secret: str):
    current_url = start_url
    
    # Limit recursion to avoid infinite loops
    for step in range(10): 
        print(f"\n=== Step {step + 1} | Processing URL: {current_url} ===")
        
        # 1. Scrape the Page
        page_content = ""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(current_url)
                # Wait for potential JS rendering
                try:
                    await page.wait_for_selector("body", timeout=5000)
                    await asyncio.sleep(2) 
                except:
                    pass 
                page_content = await page.content()
                await browser.close()
        except Exception as e:
            print(f"Scraping Error: {e}")
            break

        # 2. Analyze with Gemini (Get Task & Submit URL)
        prompt_analysis = f"""
        You are an autonomous bot. Analyze this HTML page:
        
        {page_content[:20000]} 
        
        Extract:
        1. The exact submission URL (often mentioned near "Post your answer to...").
        2. The precise question or task to solve.
        
        Return ONLY valid JSON: {{ "submit_url": "...", "question": "..." }}
        """
        
        analysis = None
        # Retry loop for 429 errors
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=MODEL_NAME,
                    contents=prompt_analysis,
                    config=types.GenerateContentConfig(response_mime_type="application/json")
                )
                analysis = parse_json_response(response.text)
                break
            except Exception as e:
                print(f"GenAI Analysis Error (Attempt {attempt+1}): {e}")
                time.sleep(5) # Wait before retry

        if not analysis or not analysis.get("submit_url"):
            print("Failed to analyze page or find submit URL.")
            break

        submit_url = analysis.get("submit_url")
        question = analysis.get("question")
        print(f"Task: {question}")
        print(f"Submit Target: {submit_url}")

        # 3. Generate Code to Solve
        prompt_code = f"""
        Write Python code to solve this: "{question}".
        
        Context:
        - If needing files, download using `requests`.
        - If parsing PDF, use `PyPDF2`.
        - If analyzing data, use `pandas`.
        - Define a function `get_answer()` that returns the answer.
        - Return the answer exactly as requested (int, string, bool).
        
        Return ONLY Python code.
        """
        
        generated_code = ""
        try:
            code_response = client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt_code
            )
            generated_code = code_response.text
        except Exception as e:
            print(f"GenAI Coding Error: {e}")
            break
        
        # 4. Execute Code
        answer = execute_generated_code(generated_code)
        print(f"Calculated Answer: {answer}")

        # 5. Submit Answer
        payload = {
            "email": email,
            "secret": secret,
            "url": current_url,
            "answer": answer
        }
        
        try:
            # Handle relative URLs
            if not submit_url.startswith("http"):
                # Simple join for relative paths
                base_parts = current_url.split("/")
                base_domain = f"{base_parts[0]}//{base_parts[2]}"
                submit_url = base_domain + submit_url if submit_url.startswith("/") else f"{current_url}/{submit_url}"

            print(f"Posting to: {submit_url}")
            res = requests.post(submit_url, json=payload, timeout=15)
            res_json = res.json()
            print(f"Server Response: {res_json}")
            
            # 6. Check for Next Step
            if res_json.get("correct") is True:
                next_url = res_json.get("url")
                if next_url:
                    current_url = next_url
                    continue 
                else:
                    print("Quiz Completed Successfully!")
                    break
            else:
                print("Answer incorrect. Stopping.")
                # Optional: Retry logic could go here
                break
                
        except Exception as e:
            print(f"Submission failed: {e}")
            break

# --- API Endpoints ---
@app.post("/")
async def start_quiz(req: Request, background_tasks: BackgroundTasks):
    try:
        body = await req.json()
    except:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    email = body.get("email")
    secret = body.get("secret")
    start_url = body.get("url")
    
    # Basic Validation
    if not email or not secret or not start_url:
        raise HTTPException(status_code=400, detail="Missing fields")

    # Start Background Task
    background_tasks.add_task(solve_quiz_recursive, start_url, email, secret)

    return {"message": "Quiz processing started", "status": "ok"}

@app.get("/health")
def health():
    return {"status": "active"}