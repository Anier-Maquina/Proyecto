# Control horario

El proyecto incluye un servidor Python sin dependencias externas, una base SQLite persistente y las dos vistas existentes: el portal de administración y el fichaje móvil.

## Rutas en Netlify

El archivo `_redirects` configura la web de marketing (`Página Web de la Empresa.html`) como página principal y permite abrir el acceso al portal en `https://serviciosgrupoada.es/login`. El panel administrador también se puede instalar como PWA independiente desde `https://serviciosgrupoada.es/admin/`; su service worker solo controla `/admin/` y no almacena en caché datos privados ni respuestas de API. Esta PWA no cambia la web pública ni la app móvil de trabajadores. El archivo `_redirects` también mantiene las rutas del panel y del fichaje de operarios. Las solicitudes `/api/*` se reenvían desde Netlify a Render mediante `netlify/functions/api.js`, conservando método, parámetros, cuerpo, cookies y respuesta. El destino predeterminado es `https://proyecto-87o9.onrender.com`; si cambia la URL del servicio, actualízala con la variable opcional `ATTENDANCE_API_ORIGIN` en Netlify, usando solo el origen HTTP(S), sin rutas ni barras finales. Al ejecutar localmente el servidor Python, el frontend llama directamente a sus rutas `/api/*`.

Para instalar el panel en Android, publica los cambios en Netlify, abre `https://serviciosgrupoada.es/admin/` en Chrome e inicia sesión con la cuenta de administrador. En el menú de Chrome, selecciona **Instalar aplicación** o **Añadir a pantalla de inicio**. El acceso instalado abre el panel; la app de fichaje de trabajadores conserva su enlace y funcionamiento propios.

El inicio de sesión y el almacenamiento de datos dependen de que `server.py` se ejecute en el servidor configurado, con acceso a SQLite; Netlify no ejecuta ese servidor Python al publicar los archivos estáticos. Las rutas de documentos de firma también pasan por el proxy `/api/*` y se reenvían al backend para evitar los límites de tamaño de las funciones de Netlify, tanto al cargar archivos como al servir la vista previa PDF.

## Requisitos

- Python 3.10 o posterior.
- Un navegador moderno. La geolocalización funciona en `localhost` o en un origen servido mediante HTTPS.
- Acceso a Internet para descargar Tailwind CSS y Leaflet/OpenStreetMap.
- Node.js y npm para desarrollar la aplicación móvil de `mobile-worker`.

## Aplicación móvil privada para trabajadores

`mobile-worker` contiene la app Expo para Android e iPhone. El trabajador inicia sesión con su nombre completo y PIN de cuatro dígitos, inicia o finaliza su jornada y puede compartir su última posición GPS con el panel web mientras la jornada está activa. El token Bearer se guarda en el almacenamiento seguro del dispositivo y el backend lo revoca al cerrar sesión. La app móvil solo acepta cuentas de trabajadores.

La app solicita permiso de ubicación «Siempre»/en segundo plano y muestra el estado del seguimiento. En Android mantiene una notificación de servicio en primer plano; en iPhone activa el indicador del sistema para ubicación en segundo plano. Envía actualizaciones como máximo cada minuto o cada 50 metros, según el sistema y la disponibilidad del GPS. El servidor conserva solo la posición más reciente, no el recorrido. Fichar salida detiene el servicio y borra la posición en vivo. El sistema operativo puede suspender o limitar el GPS, por ejemplo, si el usuario fuerza el cierre de la app o activa restricciones de batería; la distribución nativa no garantiza señal GPS cuando el dispositivo no puede obtener ubicación o conectividad.

Para preparar la compilación privada:

```powershell
cd .\mobile-worker
npm install
npx eas-cli login
npx eas-cli build:configure
npx eas-cli build --profile apk --platform android
```

La primera configuración de EAS debe asociar el proyecto a una cuenta Expo. El perfil `apk` genera un APK Android instalable internamente; EAS mostrará un enlace para descargarlo al terminar la compilación. El perfil `preview` también permite generar una compilación iOS de distribución interna. Para instalar en iPhone con distribución Ad Hoc, la cuenta Apple Developer debe registrar los dispositivos de los trabajadores; la instalación privada en iOS no funciona igual que distribuir un APK Android. Para pruebas locales hace falta un dispositivo físico y una compilación nativa/de desarrollo: Expo Go no sustituye la configuración nativa de ubicación en segundo plano. Usa solo dispositivos autorizados y explica a los trabajadores cuándo se comparte la ubicación antes de distribuir la app.

## Iniciar el servidor e iniciar sesión

Desde la carpeta del proyecto:

```powershell
python .\server.py serve
```

En Windows, también puedes iniciar `Iniciar_Proyecto.bat`; este inicia el servidor escuchando en la red local, espera a que esté disponible y abre la página de acceso general para elegir el rol, incluso si había una sesión previa. Desde otro dispositivo conectado a la misma red, abre `http://<IP-del-ordenador>:8000` (puedes consultar la IP local con `ipconfig`). Si Windows Firewall lo pregunta, permite el acceso solo en redes privadas.

La primera ejecución crea automáticamente la cuenta administrativa `administrador` con PIN `7804` y 30 cuentas de operario: diez PINs por departamento. Abre `http://127.0.0.1:8000` e inicia sesión. Administración debe introducir exactamente `administrador` y `7804`; cada operario debe escribir el nombre completo exacto, respetando mayúsculas y espacios, asociado a su PIN de cuatro dígitos. El sistema dirige administración a `/admin/dashboard.html` y los operarios a `/worker/fichar.html`.

Si se introduce un PIN incorrecto cinco veces con el usuario `administrador`, la cuenta administrativa queda bloqueada hasta desbloquearla manualmente desde el PC servidor. Detén el servidor, abre PowerShell en la carpeta del proyecto y ejecuta:

```powershell
python .\server.py unlock-admin
```

Después, vuelve a iniciar el servidor con `python .\server.py serve` o `Iniciar_Proyecto.bat`. El bloqueo solo afecta a la cuenta `administrador`; los intentos de inicio de sesión de los operarios siguen su límite habitual por dirección IP.

En el panel lateral, **Bloqueo intentos fallidos** abre la vista dedicada al registro de los últimos 200 rechazos de acceso administrativo, con fecha y hora UTC, IP y motivo. Un administrador puede bloquear o desbloquear una dirección IPv4 o IPv6 desde esa vista. El bloqueo por IP impide nuevos inicios de sesión de administrador desde esa dirección, pero no bloquea los fichajes ni las cuentas de operario. La lista completa se conserva en SQLite.

En el panel lateral, **Calculadora de ganancias** permite modificar el precio alternativo por hora y los costes, añadir conceptos de gasto y calcular ganancias por servicio o mensuales. El total propuesto por la comunidad, cuando se introduce, se divide entre las horas del servicio para obtener la tarifa efectiva; si se deja vacío, se utiliza el precio por hora. La columna mensual de cada concepto se calcula a partir de su importe por hora, las horas por servicio y el número de servicios al mes.

El servidor registra la dirección remota que ve en la conexión. Si la aplicación está detrás de un proxy, o varios usuarios comparten una IP pública, la IP puede representar al proxy o a todos esos usuarios; por eso conviene comprobarla antes de bloquearla. El bloqueo de la cuenta administrativa tras cinco PIN incorrectos es independiente y se sigue reactivando con `python .\server.py unlock-admin` desde el PC servidor.

**Firma Electrónica:** en la opción del panel lateral, inicia sesión como administrador, añade un PDF de empresa (máximo 10 MB) y selecciónalo para ver la vista previa. Instala AutoFirma desde [la página oficial de descargas](https://firmaelectronica.gob.es/descargas), conecta el lector DNIe y pulsa **Firmar con DNI Electrónico**. AutoFirma crea un PDF PAdES localmente y el navegador lo descarga en la carpeta de descargas configurada. Los documentos originales se guardan en `Almacenamiento/documentos_firma`; el PDF firmado no se envía al servidor. La base de datos conserva el estado y huellas SHA-256, no datos del certificado. El indicador de firma registra el resultado comunicado por el navegador: el servidor no valida criptográficamente el PDF descargado. La integración cliente usa AutoScript 1.10.1 del [repositorio oficial Cliente @firma](https://github.com/ctt-gob-es/clienteafirma), que se carga desde GitHub cuando se inicia la firma; se necesita conexión a Internet y un contexto seguro (HTTPS o localhost).

PINs iniciales:

| Departamento | PINs |
|---|---|
| Limpieza | `4101`–`4110` |
| Conserjería | `5201`–`5210` |
| Jardinería | `6301`–`6310` |

Los nombres iniciales son marcadores editables. Desde **Estructura Empresa**, el administrador puede buscar trabajadores, consultar y editar nombre, cargo, contacto, departamento y comunidades asignadas. El PIN permanece oculto hasta pulsar el botón del ojo. **Generar PIN** asigna un PIN aleatorio de cuatro cifras que no esté en uso al trabajador elegido; **Añadir trabajador** crea una cuenta nueva con otro PIN único. Los perfiles y cambios quedan guardados en SQLite y los nuevos nombres se reflejan en el fichaje y en el panel de operarios activos. Cambiar el PIN revoca las sesiones anteriores de ese trabajador. El inicio de sesión limita los intentos fallidos por dirección IP.

**Seguridad:** no publiques esta lista de PINs. El PIN administrativo solicitado `7804` es una credencial fija en esta versión. Protege el acceso al archivo `Almacenamiento/control_horario.sqlite3`, ya que la gestión de Estructura permite a la administración consultar los PINs. El límite de intentos reduce la adivinación, pero un PIN de cuatro dígitos no sustituye a una contraseña robusta.

La aplicación dirige al personal a las vistas según su rol. Las rutas y las API comprueban la sesión y el rol en el servidor. El login por PIN compara exactamente el nombre mostrado y el PIN asignado; no acepta el nombre de un departamento como identidad ni el inicio de sesión heredado por contraseña.

El servidor escucha por defecto en `0.0.0.0:3000` (o en el puerto indicado por la variable de entorno `PORT`); en Render, inicia el servicio con `python server.py serve`. Las rutas públicas `/ping` y `/health` responden con el estado online sin consultar la base de datos. Render activa automáticamente las cookies HTTPS seguras; en otros despliegues puedes configurar `ATTENDANCE_SECURE_COOKIE=1`. Si usas un dominio frontend distinto de `https://serviciosgrupoada.es`, `https://www.serviciosgrupoada.es` o `https://stalwart-pudding-467a32.netlify.app`, configura `ATTENDANCE_CORS_ORIGINS` con los orígenes permitidos separados por comas. También puedes especificar `python .\server.py serve --host 0.0.0.0 --port 8000` en un entorno local. Limita el acceso al servidor a redes privadas de confianza: la cuenta administrativa usa un PIN fijo y las cookies no tienen HTTPS en la configuración HTTP local. No expongas el servidor HTTP directamente a Internet; usa un proxy inverso con HTTPS. La geolocalización del navegador requiere HTTPS fuera de `localhost`, aunque el fichaje permite continuar sin coordenadas si no se puede obtener GPS.

Sin `DATABASE_URL`, la base se crea en `Almacenamiento/control_horario.sqlite3`. Si configuras `DATABASE_URL`, el servidor usa PostgreSQL y crea allí el esquema; esta selección no migra automáticamente los datos que ya existían en SQLite. Los documentos PDF e informes siguen guardándose como archivos en el sistema local del servicio, no en PostgreSQL, por lo que requieren almacenamiento persistente aparte en Render. Los fichajes guardan el identificador, nombre y departamento del operario, el tipo de evento, la hora UTC recibida por el servidor y la ubicación GPS cuando está disponible con una precisión de hasta 100 metros. El navegador solicita alta precisión, espera hasta 10 segundos y no reutiliza posiciones antiguas; si no obtiene una ubicación suficientemente precisa, permite registrar el fichaje sin coordenadas y muestra un aviso. Durante una jornada activa, la pantalla de fichaje abierta y visible puede compartir actualizaciones GPS con el panel de administración; se conserva únicamente la última posición, no el recorrido, y el seguimiento se pausa al ocultar la página, cerrar sesión o finalizar la jornada. Se requiere permiso de ubicación y el panel puede tardar hasta 10 segundos en reflejar un nuevo punto. Cada inicio y cierre de jornada actualiza el JSON estructurado y el informe imprimible del trabajador en `Almacenamiento/control de fichaje`; ambos incluyen fecha, hora de entrada y hora de salida cuando ya está registrada. Los informes ya no se muestran en **Archivo e Impresión**; allí se mantiene el archivo documental. Administración puede corregir nombre, fecha y horas desde **Control horario**; la edición actualiza la base de datos y los archivos.

La sección **Control mensual de gastos e ingresos** calcula los totales a medida que se introducen las cantidades y solo contacta con la API al pulsar **Guardar / Registrar mes**. El servidor valida y vuelve a calcular los totales y el beneficio, y guarda un registro JSON por mes en `Almacenamiento/control mensual de gastos e ingresos`; si el backend no está disponible, la interfaz conserva los datos y muestra un aviso discreto sin bloquearla. Los registros guardados también se muestran en **Archivo e Impresión**.

La web pública incluye el formulario **Trabaja con Nosotros**, que envía los datos al backend y permite adjuntar opcionalmente un currículum PDF (máximo 4 MB). Las candidaturas se consultan en **Candidaturas**, debajo de **Bloqueo intentos fallidos**, y requieren sesión de administrador; desde allí se puede descargar el CV cuando se adjuntó o eliminar la solicitud y su archivo. Los PDF se guardan fuera del sitio público en `solicitudes_empleo`, junto al archivo indicado por `ATTENDANCE_DATABASE` (por defecto, dentro de `Almacenamiento`), por lo que necesitan almacenamiento persistente en Render, incluso al usar PostgreSQL.

El formulario público de presupuesto guarda sus solicitudes en el backend y solo confirma el envío cuando se recibe una respuesta correcta. En el panel, **Clientes** (entre **Estructura Empresa** y **Gestión Inventario**) permite buscar fichas por nombre o dirección, crear clientes, consultar/eliminar solicitudes de presupuesto recibidas y usar un calculador orientativo de horas, jardinería, limpieza post-obra y limpieza mensual comunitaria. La tarifa mensual estimada es de 170 €, 310 €, 430 €, 540 € y 620 € para 1, 2, 3, 4 y 5 días semanales, respectivamente. El calculador y las fichas de cliente solo se pueden administrar con sesión de administrador. El botón para compartir el calculador permanece desactivado hasta que se publique una versión pública.

## Validación

```powershell
python -m unittest discover -s tests -v
```
