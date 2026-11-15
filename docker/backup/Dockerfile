# The official MariaDB 11.4.13 image, pinned to its multi-platform manifest.
FROM mariadb:11.4@sha256:1292844148b311e4ed4300022a996d39083f415a963e970cf47cad1b3b18e3a6

# Client binaries come from MariaDB itself. Install only declared distribution
# packages at build time; never fetch scripts or install anything at startup.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 gzip coreutils util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/db_backup.py /opt/minikandoo/db_backup.py
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=UTC

# Replaces the database image entrypoint: no database server is ever started.
ENTRYPOINT ["python3", "/opt/minikandoo/db_backup.py"]
CMD ["schedule"]
