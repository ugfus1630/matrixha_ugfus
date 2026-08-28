ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /opt/matrix_bridge

# matrix-nio[e2e] 0.26 resolves to vodozemac wheels on amd64/aarch64, so this
# stays a pure wheel install with no build dependencies.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
