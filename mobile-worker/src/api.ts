export const API_ORIGIN = 'https://proyecto-87o9.onrender.com';
export const TOKEN_STORAGE_KEY = 'grupo-ada-worker-token';

export type WorkerUser = {
  id: number;
  username: string;
  display_name: string;
  role: 'worker';
  first_name: string;
  last_name: string;
  department: string | null;
};

export type AttendanceEvent = {
  event_type: 'start' | 'stop';
  recorded_at: string;
  location_available: number;
};

export type AttendanceState = {
  active: boolean;
  events: AttendanceEvent[];
};

export type LocationPayload = {
  latitude: number;
  longitude: number;
  accuracy: number;
  locationTimestamp: string;
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export async function apiRequest<T>(
  path: string,
  token?: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_ORIGIN}${path}`, {
      ...options,
      headers: {
        Accept: 'application/json',
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
    });
  } catch (error) {
    if (error instanceof TypeError) {
      throw new Error('No se pudo conectar con el servidor. Comprueba tu conexión e inténtalo de nuevo.');
    }
    throw error;
  }

  const contentType = response.headers.get('content-type') ?? '';
  const body: unknown = contentType.includes('application/json')
    ? await response.json()
    : null;
  if (!response.ok) {
    const message =
      typeof body === 'object' &&
      body !== null &&
      'error' in body &&
      typeof body.error === 'string'
        ? body.error
        : `El servidor respondió con el error ${response.status}.`;
    throw new ApiError(message, response.status);
  }
  if (!body || typeof body !== 'object') {
    throw new Error('El servidor devolvió una respuesta no válida.');
  }
  return body as T;
}
