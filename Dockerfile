FROM public.ecr.aws/lambda/python:3.11

WORKDIR /app

COPY requirements.txt .
# Upgrade pip antes de instalar dependencias.
# La imagen Lambda Python 3.11 trae pip 24.0 (inicios 2024) que NO conoce los
# wheels modernos de numpy 2.x → baja la source distribution e intenta
# compilar, lo cual falla porque la imagen Lambda no incluye compilador C.
# Con pip >=25 resuelve los wheels manylinux_2_28_x86_64 correctamente.
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt -t /app/deps

ENV PYTHONPATH=/app/deps:/app

COPY agent/ agent/
COPY api/ api/
COPY main.py .
COPY lambda_twilio_webhook.py .

EXPOSE 8000

ENTRYPOINT []
CMD ["python", "main.py"]
