import * as SecureStore from 'expo-secure-store';
import * as TaskManager from 'expo-task-manager';
import type * as Location from 'expo-location';
import { API_ORIGIN, TOKEN_STORAGE_KEY } from './api';

export const LOCATION_TASK_NAME = 'grupo-ada-worker-background-location';

type LocationTaskData = {
  locations?: Location.LocationObject[];
};

TaskManager.defineTask<LocationTaskData>(LOCATION_TASK_NAME, async ({ data, error }) => {
  if (error || !data?.locations?.length) return;

  try {
    const location = data.locations[data.locations.length - 1];
    const token = await SecureStore.getItemAsync(TOKEN_STORAGE_KEY);
    if (!token) return;

    const response = await fetch(`${API_ORIGIN}/api/attendance/location`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        latitude: location.coords.latitude,
        longitude: location.coords.longitude,
        accuracy: location.coords.accuracy ?? 10_000,
        locationTimestamp: new Date(location.timestamp).toISOString(),
      }),
    });

    if (response.status === 401 || response.status === 409) {
      const LocationModule = await import('expo-location');
      if (await LocationModule.hasStartedLocationUpdatesAsync(LOCATION_TASK_NAME)) {
        await LocationModule.stopLocationUpdatesAsync(LOCATION_TASK_NAME);
      }
    }
  } catch (taskError) {
    console.warn('No se pudo enviar la ubicación en segundo plano.', taskError);
  }
});

export async function stopBackgroundLocation(): Promise<void> {
  if (await TaskManager.isTaskRegisteredAsync(LOCATION_TASK_NAME)) {
    const LocationModule = await import('expo-location');
    if (await LocationModule.hasStartedLocationUpdatesAsync(LOCATION_TASK_NAME)) {
      await LocationModule.stopLocationUpdatesAsync(LOCATION_TASK_NAME);
    }
  }
}
