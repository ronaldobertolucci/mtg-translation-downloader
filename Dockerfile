FROM python:3.12-slim
WORKDIR /app
COPY mtg_translation_downloader ./mtg_translation_downloader
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "mtg_translation_downloader"]
