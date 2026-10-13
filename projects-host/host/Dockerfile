FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libxrender1 libxext6 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir cadquery==2.6.1 cryptography==46.0.3
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /srv
USER 99:100
EXPOSE 9070 9071
CMD ["python", "/srv/supervisor.py"]
