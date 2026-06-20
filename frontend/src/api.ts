export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!headers.has("Content-Type") && options.body != null) headers.set("Content-Type", "application/json");

  const response = await fetch(path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = typeof data.error === "string" ? data.error : `${response.status} ${response.statusText}`;
    throw new ApiError(message, response.status);
  }
  return data as T;
}

export function isUnauthorized(error: unknown) {
  return error instanceof ApiError && (error.status === 401 || error.message === "unauthorized");
}
