# A2A-Agenten mit Weboberfläche – reiner CPU-Betrieb, keine GPU nötig.
#
# Das Sprachmodell wird bewusst NICHT ins Image gebacken (das wären ~3 GB extra).
# Es wird beim ersten Start nach /cache/huggingface geladen; im docker-compose.yml
# liegt dort ein Volume, damit der Download nur einmal passiert.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/cache/huggingface \
    A2A_HOST=0.0.0.0 \
    A2A_PORT=9999

WORKDIR /app

# torch zuerst und ausdrücklich aus dem CPU-Index: Der Standard-Index zieht die
# CUDA-Pakete mit und bläht das Image um mehrere Gigabyte auf, die auf einem
# Server ohne GPU niemand braucht.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch

COPY requirements.txt .
# torch steht schon oben, hier nur der Rest.
RUN grep -v '^torch$' requirements.txt > /tmp/req.txt && pip install -r /tmp/req.txt

COPY *.py ./
COPY web/ ./web/
COPY memory/ ./memory/

# Nicht als root laufen. /cache und /app/memory müssen dem Nutzer gehören,
# weil Modell-Download und Gedächtnis dorthin schreiben.
RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /cache/huggingface /app/downloads \
    && chown -R agent:agent /cache /app
USER agent

EXPOSE 9999

# Der Healthcheck fragt die Status-Route. Die antwortet sofort, auch während das
# Modell noch lädt – genau das soll er ja unterscheiden können.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['A2A_PORT']}/api/status\", timeout=4)"

CMD ["python", "web_server.py"]
