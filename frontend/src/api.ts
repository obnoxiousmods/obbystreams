export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, token: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  if (token) headers.set("x-obbystreams-token", token);

  const response = await fetch(path, {
    ...options,
    headers,
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
