# icp-scout in a container.
#
# What this image is and is not: it carries the code and its six
# dependencies, and nothing else. The data the pipeline works from -
# res_data.csv (517 MB), companies.db, archive.db, the snapshots - is
# deliberately NOT baked in. It is regenerated from public registers,
# it is in .gitignore for that reason, and an image carrying a copy of
# the Czech business register would be both enormous and stale the day
# after it is built. It is mounted instead, and the container says so
# when it is missing.
#
# Build:
#   docker build -t icp-scout .
#
# Run (the data directory is the one thing it needs from the host):
#   docker run --rm -p 8000:8000 \
#     -v "$(pwd)/data:/app/data" \
#     -e OPENAI_API_KEY=sk-... \
#     icp-scout
#
# Then http://127.0.0.1:8000/
#
# First time on an empty data directory, build the prerequisites first:
#   docker run --rm -v "$(pwd)/data:/app/data" icp-scout \
#     python -m pipeline.run --bootstrap
#
# One weekly run instead of the interface:
#   docker run --rm -v "$(pwd)/data:/app/data" \
#     -e OPENAI_API_KEY=sk-... icp-scout python -m pipeline.run

FROM python:3.14-slim

# Bytecode files in a container are written once and read once; the
# unbuffered flag is what makes `docker logs` show a run's progress while
# it is still going rather than in one lump at the end.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # The pipeline prints Czech to stderr, and a container's default
    # encoding is not always UTF-8 - the run log arrived as mojibake
    # without this once already.
    PYTHONIOENCODING=utf-8

WORKDIR /app

# Dependencies before the source, so editing a module does not reinstall
# six packages. requirements.txt changes far less often than the code.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the prototype actually runs. docs/ holds RTsoft's assignment
# and their ICP and is not shipped anywhere; data/ is mounted, not copied.
COPY pipeline/ ./pipeline/
COPY api/ ./api/
COPY web/ ./web/

# Not root: the container writes to /app/data, and a bind mount plus a
# root process is how a host directory ends up owned by root afterwards.
RUN useradd --create-home --uid 1000 scout \
    && chown -R scout:scout /app
USER scout

EXPOSE 8000

# 0.0.0.0, not 127.0.0.1: bound to loopback inside the container, the
# port would be published and still refuse every connection from the host.
#
# No --reload here. It is what a local dev launcher runs with; in an
# image it would watch files that nobody is editing and restart the
# server in the middle of a run.
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
