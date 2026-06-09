FROM public.ecr.aws/lambda/python:3.11

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -t /app/deps

ENV PYTHONPATH=/app/deps:/app

COPY agent/ agent/
COPY api/ api/
COPY main.py .
COPY lambda_twilio_webhook.py .

EXPOSE 8000

ENTRYPOINT []
CMD ["python", "main.py"]
