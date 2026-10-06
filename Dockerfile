FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VCD_HOST=0.0.0.0 \
    VCD_PORT=8080

WORKDIR /app

# 纯标准库实现，无需第三方依赖
COPY vcd_service ./vcd_service
COPY tests ./tests
COPY scripts ./scripts
RUN chmod +x ./scripts/smoke_http.py ./scripts/verify.sh \
    && python3 -m compileall -q vcd_service scripts tests

EXPOSE 8080

HEALTHCHECK --interval=2s --timeout=3s --retries=15 --start-period=2s \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status == 200 else 1)"

CMD ["python3", "-m", "vcd_service"]
