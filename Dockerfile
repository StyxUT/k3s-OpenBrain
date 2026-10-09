FROM denoland/deno:2.3.3
WORKDIR /app
ENV DENO_DIR=/deno-dir
COPY app/deno.json /app/deno.json
COPY app/index.ts /app/index.ts
COPY app/embedding.ts /app/embedding.ts
RUN deno cache /app/index.ts
CMD ["run", "--cached-only", "--allow-net", "--allow-env", "--allow-read", "/app/index.ts"]
