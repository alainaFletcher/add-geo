# Address Geocoding Application
# Dockerfile for containerized deployment

FROM python:3.13-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV CONSOLE_FORMAT=json

# Set working directory
WORKDIR /app

# Install system dependencies for pyodbc (Microsoft ODBC Driver)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg2 \
    apt-transport-https \
    ca-certificates \
    unixodbc \
    unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

# Add Microsoft repository and install ODBC Driver 18
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list | tee /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Copy vendored dependencies (app_logger is not on PyPI)
COPY vendor/*.whl ./

# Install Python dependencies including vendored wheels
RUN pip install --no-cache-dir *.whl -r requirements.txt \
    && rm -f *.whl

# Copy application code
COPY GeocodeAddresses.py .

# Create logs directory
RUN mkdir -p /app/logs

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# Run the application
CMD ["python", "GeocodeAddresses.py"]
