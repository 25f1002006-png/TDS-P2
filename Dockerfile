FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# 1. Copy requirements first (for caching)
COPY requirements.txt .

# 2. Install Python dependencies
# This relies on playwright==1.40.0 being in your requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 3. Copy the application code
# This is the step that updates your container with the new main.py
COPY main.py .

# 4. Expose the port
EXPOSE 8000

# 5. Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]