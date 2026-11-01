import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  AppState,
  type AppStateStatus,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { StatusBar } from 'expo-status-bar';
import * as Location from 'expo-location';
import * as SecureStore from 'expo-secure-store';
import { ApiError, apiRequest, TOKEN_STORAGE_KEY, type AttendanceState, type LocationPayload, type WorkerUser } from './src/api';
import { LOCATION_TASK_NAME, stopBackgroundLocation } from './src/locationTask';

type LoginResponse = {
  user: WorkerUser;
  token: string;
  tokenType: 'Bearer';
};

type SessionResponse = {
  user: WorkerUser;
};

type AttendanceResponse = AttendanceState;

const BACKGROUND_LOCATION_OPTIONS: Location.LocationTaskOptions = {
  accuracy: Location.Accuracy.High,
  timeInterval: 60_000,
  distanceInterval: 50,
  deferredUpdatesInterval: 60_000,
  deferredUpdatesDistance: 50,
  pausesUpdatesAutomatically: false,
  showsBackgroundLocationIndicator: true,
  activityType: Location.ActivityType.OtherNavigation,
  foregroundService: {
    notificationTitle: 'Ubicación de jornada activa',
    notificationBody: 'Grupo ADA comparte la última ubicación del trabajador con el panel autorizado.',
    notificationColor: '#1d5699',
  },
};

function formatTime(value?: string): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('es-ES');
}

export default function App() {
  const [username, setUsername] = useState('');
  const [pin, setPin] = useState('');
  const [token, setToken] = useState<string | null>(null);
  const [worker, setWorker] = useState<WorkerUser | null>(null);
  const [attendance, setAttendance] = useState<AttendanceState | null>(null);
  const [message, setMessage] = useState('');
  const [messageIsError, setMessageIsError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [trackingActive, setTrackingActive] = useState(false);

  const showMessage = useCallback((nextMessage: string, isError = false) => {
    setMessage(nextMessage);
    setMessageIsError(isError);
  }, []);

  const refreshAttendance = useCallback(async (sessionToken: string) => {
    const result = await apiRequest<AttendanceResponse>('/api/attendance/mine', sessionToken);
    setAttendance(result);
    return result;
  }, []);

  const enableBackgroundTracking = useCallback(async (): Promise<boolean> => {
    try {
      const available = await Location.isBackgroundLocationAvailableAsync();
      if (!available) {
        setTrackingActive(false);
        showMessage('Este dispositivo no ofrece seguimiento GPS en segundo plano.', true);
        return false;
      }

      let foreground = await Location.getForegroundPermissionsAsync();
      if (!foreground.granted) foreground = await Location.requestForegroundPermissionsAsync();
      if (!foreground.granted) {
        setTrackingActive(false);
        showMessage('Para enviar ubicaciones durante la jornada, permite el acceso a la ubicación.', true);
        return false;
      }

      let background = await Location.getBackgroundPermissionsAsync();
      if (!background.granted) background = await Location.requestBackgroundPermissionsAsync();
      if (!background.granted) {
        setTrackingActive(false);
        showMessage(
          Platform.OS === 'ios'
            ? 'Permite el acceso a la ubicación «Siempre» en Ajustes para continuar con la pantalla bloqueada.'
            : 'Permite la ubicación en segundo plano en Ajustes para continuar con la pantalla bloqueada.',
          true,
        );
        return false;
      }

      if (!(await Location.hasStartedLocationUpdatesAsync(LOCATION_TASK_NAME))) {
        await Location.startLocationUpdatesAsync(LOCATION_TASK_NAME, BACKGROUND_LOCATION_OPTIONS);
      }
      setTrackingActive(true);
      showMessage('Seguimiento activo. La app compartirá la última posición durante la jornada.');
      return true;
    } catch (error) {
      setTrackingActive(false);
      showMessage(
        error instanceof Error ? error.message : 'No se pudo activar el seguimiento en segundo plano.',
        true,
      );
      return false;
    }
  }, [showMessage]);

  const loadSession = useCallback(
    async (sessionToken: string) => {
      const result = await apiRequest<SessionResponse>('/api/mobile/session', sessionToken);
      if (result.user.role !== 'worker') throw new Error('La app móvil es solo para trabajadores.');
      setWorker(result.user);
      setToken(sessionToken);
      const currentAttendance = await refreshAttendance(sessionToken);
      if (currentAttendance.active) await enableBackgroundTracking();
    },
    [enableBackgroundTracking, refreshAttendance],
  );

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const savedToken = await SecureStore.getItemAsync(TOKEN_STORAGE_KEY);
        if (savedToken) await loadSession(savedToken);
      } catch (error) {
        await SecureStore.deleteItemAsync(TOKEN_STORAGE_KEY);
        await stopBackgroundLocation();
        if (error instanceof ApiError && error.status === 401) return;
        showMessage(error instanceof Error ? error.message : 'No se pudo recuperar la sesión.', true);
      } finally {
        if (active) setInitializing(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [loadSession, showMessage]);

  useEffect(() => {
    if (!token || !attendance?.active) return;
    const interval = setInterval(() => {
      void refreshAttendance(token).catch((error: unknown) => {
        if (error instanceof ApiError && error.status === 401) {
          void signOut(false);
        }
      });
    }, 60_000);
    return () => clearInterval(interval);
  }, [attendance?.active, refreshAttendance, token]);

  useEffect(() => {
    const onAppStateChange = (nextState: AppStateStatus) => {
      if (nextState !== 'active' || !token) return;
      void refreshAttendance(token)
        .then(async (result) => {
          if (result.active) {
            await enableBackgroundTracking();
          } else {
            await stopBackgroundLocation();
            setTrackingActive(false);
          }
        })
        .catch(() => undefined);
    };
    const subscription = AppState.addEventListener('change', onAppStateChange);
    return () => subscription.remove();
  }, [enableBackgroundTracking, refreshAttendance, token]);

  async function signIn(): Promise<void> {
    const cleanUsername = username.trim();
    if (!cleanUsername || !/^\d{4}$/.test(pin)) {
      showMessage('Escribe tu nombre completo y el PIN de 4 dígitos.', true);
      return;
    }

    setBusy(true);
    showMessage('');
    try {
      const result = await apiRequest<LoginResponse>('/api/mobile/login', undefined, {
        method: 'POST',
        body: JSON.stringify({ username: cleanUsername, pin }),
      });
      if (result.user.role !== 'worker' || !result.token) {
        throw new Error('La app móvil solo admite cuentas de trabajador.');
      }
      await SecureStore.setItemAsync(TOKEN_STORAGE_KEY, result.token);
      setPin('');
      await loadSession(result.token);
      showMessage('Sesión iniciada correctamente.');
    } catch (error) {
      showMessage(error instanceof Error ? error.message : 'No se pudo iniciar sesión.', true);
    } finally {
      setBusy(false);
    }
  }

  async function getCurrentLocation(): Promise<LocationPayload | null> {
    try {
      let permission = await Location.getForegroundPermissionsAsync();
      if (!permission.granted) permission = await Location.requestForegroundPermissionsAsync();
      if (!permission.granted) return null;
      const result = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.High });
      return {
        latitude: result.coords.latitude,
        longitude: result.coords.longitude,
        accuracy: result.coords.accuracy ?? 10_000,
        locationTimestamp: new Date(result.timestamp).toISOString(),
      };
    } catch {
      return null;
    }
  }

  async function recordAttendance(action: 'start' | 'stop'): Promise<void> {
    if (!token) return;
    setBusy(true);
    showMessage(action === 'start' ? 'Registrando entrada...' : 'Registrando salida...');
    try {
      const location = await getCurrentLocation();
      await apiRequest(`/api/attendance/${action}`, token, {
        method: 'POST',
        body: JSON.stringify(location ?? { locationAvailable: false }),
      });
      const result = await refreshAttendance(token);
      if (result.active) {
        await enableBackgroundTracking();
      } else {
        await stopBackgroundLocation();
        setTrackingActive(false);
        showMessage('Jornada finalizada. El seguimiento GPS se ha detenido.');
      }
    } catch (error) {
      showMessage(error instanceof Error ? error.message : 'No se pudo registrar el fichaje.', true);
    } finally {
      setBusy(false);
    }
  }

  async function signOut(showConfirmation = true): Promise<void> {
    if (!token) return;
    const finish = async () => {
      setBusy(true);
      try {
        await stopBackgroundLocation();
        await apiRequest('/api/mobile/logout', token, { method: 'POST', body: '{}' });
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 401)) {
          showMessage('Sesión cerrada en este dispositivo; sin conexión, el token expirará en el servidor.', true);
        }
      } finally {
        await SecureStore.deleteItemAsync(TOKEN_STORAGE_KEY);
        setToken(null);
        setWorker(null);
        setAttendance(null);
        setTrackingActive(false);
        setBusy(false);
      }
    };

    if (!showConfirmation) {
      await finish();
      return;
    }
    Alert.alert('Cerrar sesión', 'Se detendrá el seguimiento GPS en este dispositivo.', [
      { text: 'Cancelar', style: 'cancel' },
      { text: 'Cerrar sesión', style: 'destructive', onPress: () => void finish() },
    ]);
  }

  if (initializing && !worker) {
    return (
      <View style={styles.loading}>
        <ActivityIndicator size="large" color="#1d5699" />
        <Text style={styles.muted}>Preparando fichaje…</Text>
      </View>
    );
  }

  return (
    <KeyboardAvoidingView
      style={styles.screen}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <StatusBar style="light" />
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <View style={styles.header}>
          <Text style={styles.brand}>GRUPO ADA</Text>
          <Text style={styles.title}>Fichaje del trabajador</Text>
          <Text style={styles.headerSubtitle}>Jornada y ubicación en un solo lugar</Text>
        </View>

        {worker ? (
          <View style={styles.card}>
            <Text style={styles.eyebrow}>TRABAJADOR</Text>
            <Text style={styles.workerName}>{worker.display_name}</Text>
            <Text style={styles.muted}>{worker.department ?? 'Sin departamento'}</Text>
            <View style={styles.statusBox}>
              <Text style={styles.eyebrow}>ESTADO DE JORNADA</Text>
              <Text style={[styles.status, attendance?.active ? styles.active : styles.inactive]}>
                {attendance?.active ? 'Jornada activa' : 'Fuera de jornada'}
              </Text>
              <Text style={styles.muted}>
                Entrada más reciente: {formatTime(attendance?.events.find((event) => event.event_type === 'start')?.recorded_at)}
              </Text>
            </View>
            {attendance?.active ? (
              <>
                <View style={styles.trackingNotice}>
                  <Text style={styles.trackingTitle}>
                    {trackingActive ? 'Ubicación en segundo plano activa' : 'Seguimiento GPS no activo'}
                  </Text>
                  <Text style={styles.trackingText}>
                    {trackingActive
                      ? 'Mientras la jornada esté activa, se comparte la última ubicación GPS, incluso con la pantalla bloqueada.'
                      : 'Activa los permisos de ubicación en segundo plano para compartir la última posición durante la jornada.'}{' '}
                    No se guarda el recorrido completo. El sistema puede limitar el GPS por ahorro de batería.
                  </Text>
                </View>
                <PrimaryButton
                  title="Detener jornada"
                  color="#b42318"
                  disabled={busy}
                  onPress={() => void recordAttendance('stop')}
                />
              </>
            ) : (
              <PrimaryButton
                title="Iniciar jornada"
                color="#16803c"
                disabled={busy}
                onPress={() => void recordAttendance('start')}
              />
            )}
            <SecondaryButton title="Cerrar sesión" disabled={busy} onPress={() => void signOut()} />
          </View>
        ) : (
          <View style={styles.card}>
            <Text style={styles.eyebrow}>ACCESO DE TRABAJADORES</Text>
            <Text style={styles.formTitle}>Inicia sesión</Text>
            <Text style={styles.muted}>Escribe tu nombre completo tal como aparece en el sistema y tu PIN.</Text>
            <Text style={styles.label}>Nombre completo</Text>
            <TextInput
              value={username}
              onChangeText={setUsername}
              autoCapitalize="words"
              autoCorrect={false}
              autoComplete="name"
              placeholder="Nombre y apellidos"
              placeholderTextColor="#94a3b8"
              style={styles.input}
              editable={!busy}
              returnKeyType="next"
            />
            <Text style={styles.label}>PIN de 4 dígitos</Text>
            <TextInput
              value={pin}
              onChangeText={(value) => setPin(value.replace(/\D/g, '').slice(0, 4))}
              keyboardType="number-pad"
              secureTextEntry
              placeholder="••••"
              placeholderTextColor="#94a3b8"
              style={[styles.input, styles.pinInput]}
              editable={!busy}
              maxLength={4}
              returnKeyType="done"
              onSubmitEditing={() => void signIn()}
            />
            <View style={styles.privacyNotice}>
              <Text style={styles.trackingText}>
                La app solicita permiso para compartir la última ubicación durante la jornada activa. El seguimiento se
                detiene al fichar salida o cerrar sesión.
              </Text>
            </View>
            <PrimaryButton title="Iniciar sesión" disabled={busy} onPress={() => void signIn()} />
          </View>
        )}

        {message ? (
          <View style={[styles.message, messageIsError && styles.errorMessage]}>
            <Text style={[styles.messageText, messageIsError && styles.errorText]}>{message}</Text>
          </View>
        ) : null}
        <Text style={styles.footer}>La ubicación solo se utiliza para gestionar el fichaje durante la jornada.</Text>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

function PrimaryButton({
  title,
  color = '#1d5699',
  disabled,
  onPress,
}: {
  title: string;
  color?: string;
  disabled: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [styles.primaryButton, { backgroundColor: color }, disabled && styles.disabled, pressed && styles.pressed]}
    >
      {disabled ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryButtonText}>{title}</Text>}
    </Pressable>
  );
}

function SecondaryButton({
  title,
  disabled,
  onPress,
}: {
  title: string;
  disabled: boolean;
  onPress: () => void;
}) {
  return (
    <Pressable
      accessibilityRole="button"
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [styles.secondaryButton, disabled && styles.disabled, pressed && styles.pressed]}
    >
      <Text style={styles.secondaryButtonText}>{title}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: '#f1f5f9' },
  content: { flexGrow: 1, paddingBottom: 36 },
  loading: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 12, backgroundColor: '#f1f5f9' },
  header: { backgroundColor: '#12243a', paddingTop: 64, paddingBottom: 34, paddingHorizontal: 24 },
  brand: { color: '#84c5ff', fontSize: 12, fontWeight: '800', letterSpacing: 2 },
  title: { color: '#fff', fontSize: 28, fontWeight: '800', marginTop: 10 },
  headerSubtitle: { color: '#cbd5e1', fontSize: 14, marginTop: 8 },
  card: { backgroundColor: '#fff', margin: 20, padding: 22, borderRadius: 22, gap: 14, elevation: 3 },
  eyebrow: { color: '#64748b', fontSize: 11, fontWeight: '800', letterSpacing: 1.4 },
  workerName: { color: '#172b43', fontSize: 23, fontWeight: '800' },
  formTitle: { color: '#172b43', fontSize: 22, fontWeight: '800' },
  muted: { color: '#64748b', fontSize: 14, lineHeight: 21 },
  statusBox: { marginVertical: 6, padding: 16, backgroundColor: '#f8fafc', borderRadius: 16, gap: 5 },
  status: { fontSize: 18, fontWeight: '800' },
  active: { color: '#16803c' },
  inactive: { color: '#475569' },
  trackingNotice: { backgroundColor: '#eff6ff', borderColor: '#bfdbfe', borderWidth: 1, borderRadius: 16, padding: 15, gap: 6 },
  trackingTitle: { color: '#1e40af', fontSize: 15, fontWeight: '800' },
  trackingText: { color: '#334155', fontSize: 13, lineHeight: 19 },
  primaryButton: { minHeight: 54, borderRadius: 14, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 18, marginTop: 4 },
  primaryButtonText: { color: '#fff', fontSize: 16, fontWeight: '800' },
  secondaryButton: { minHeight: 48, borderRadius: 14, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderColor: '#cbd5e1' },
  secondaryButtonText: { color: '#334155', fontSize: 15, fontWeight: '700' },
  disabled: { opacity: 0.6 },
  pressed: { opacity: 0.85 },
  label: { color: '#334155', fontSize: 14, fontWeight: '700', marginBottom: -8 },
  input: { minHeight: 52, borderWidth: 1, borderColor: '#cbd5e1', borderRadius: 13, paddingHorizontal: 14, color: '#172b43', fontSize: 16, backgroundColor: '#fff' },
  pinInput: { letterSpacing: 8, textAlign: 'center', fontSize: 20 },
  privacyNotice: { backgroundColor: '#f8fafc', borderRadius: 14, padding: 13 },
  message: { marginHorizontal: 20, padding: 14, borderRadius: 14, backgroundColor: '#ecfdf5' },
  messageText: { color: '#166534', fontSize: 14, lineHeight: 20 },
  errorMessage: { backgroundColor: '#fff1f2' },
  errorText: { color: '#9f1239' },
  footer: { color: '#64748b', textAlign: 'center', paddingHorizontal: 32, fontSize: 12, lineHeight: 18, marginTop: 'auto' },
});
