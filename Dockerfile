FROM anasty17/mltb:latest

WORKDIR /app
RUN chmod 777 /app

RUN python3 -m venv mltbenv

COPY requirements.txt .
RUN mltbenv/bin/pip install --no-cache-dir -r requirements.txt

# Download gotohp-cli for Google Photos uploads
RUN (wget -qO /usr/local/bin/gotohp-cli https://github.com/xob0t/gotohp/releases/latest/download/gotohp-cli_amd64 \
    || curl -sL -o /usr/local/bin/gotohp-cli https://github.com/xob0t/gotohp/releases/latest/download/gotohp-cli_amd64) \
    && chmod +x /usr/local/bin/gotohp-cli || true

COPY . .

RUN sed -i 's/\r$//' *.sh

CMD ["bash", "start.sh"]
