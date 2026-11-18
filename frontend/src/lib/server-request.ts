export class BodyError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

/** Enforce the limit on streamed bytes, including requests without Content-Length. */
export async function readBoundedJson(request: Request, limit = 32768): Promise<unknown> {
  if (Number(request.headers.get("content-length")) > limit) throw new BodyError("Request too large", 413);
  const reader = request.body?.getReader();
  if (!reader) throw new BodyError("JSON body required", 400);
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > limit) { await reader.cancel(); throw new BodyError("Request too large", 413); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder().decode(bytes)); }
  catch { throw new BodyError("Invalid JSON", 400); }
}
