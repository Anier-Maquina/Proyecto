# Control horario

El proyecto incluye un servidor Python sin dependencias externas, una base SQLite persistente y las dos vistas existentes: el portal de administración y el fichaje móvil.

## Rutas en Netlify

El archivo `_redirects` configura la web de marketing (`Página Web de la Empresa.html`) como página principal y permite abrir el acceso al portal en `https://serviciosgrupoada.es/login`. También mantiene las rutas del panel de administración y del fichaje de operarios. La función `netlify/functions/api.js` reenvía las solicitudes `/api/*` a `https://proyecto-87t9.onrender.com`, conservando método, parámetros, cuerpo, cookies y respuesta.

El inicio de sesión y el almacenamiento de datos siguen dependiendo de que `server.py` se ejecute en `proyecto-87t9.onrender.com` con acceso a SQLite; Netlify no ejecuta ese servidor Python al publicar los archivos estáticos. Las rutas de documentos de firma se reenvían directamente al backend para evitar los límites de tamaño de las funciones de Netlify, tanto al cargar archivos como al servir la vista previa PDF.

## Requisitos

- Python 3.10 o posterior.
- Un navegador moderno. La geolocalización funciona en `localhost` o en un origen servido mediante HTTPS.
- Acceso a Internet para descargar Tailwind CSS y Leaflet/OpenStreetMap.

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

El servidor registra la dirección remota que ve en la conexión. Si la aplicación está detrás de un proxy, o varios usuarios comparten una IP pública, la IP puede representar al proxy o a todos esos usuarios; por eso conviene comprobarla antes de bloquearla. El bloqueo de la cuenta administrativa tras cinco PIN incorrectos es independiente y se sigue reactivando con `python .\server.py unlock-admin` desde el PC servidor.

**Firma Electrónica:** en la opción del panel lateral, inicia sesión como administrador, añade un PDF de empresa (máximo 10 MB) y selecciónalo para ver la vista previa. Instala AutoFirma desde [la página oficial de descargas](https://firmaelectronica.gob.es/descargas), conecta el lector DNIe y pulsa **Firmar con DNI Electrónico**. AutoFirma crea un PDF PAdES localmente y el navegador lo descarga en la carpeta de descargas configurada. Los documentos originales se guardan en `Almacenamiento/documentos_firma`; el PDF firmado no se envía al servidor. La base de datos conserva el estado y huellas SHA-256, no datos del certificado. El indicador de firma registra el resultado comunicado por el navegador: el servidor no valida criptográficamente el PDF descargado. La integración cliente usa AutoScript 1.10.1 del [repositorio oficial Cliente @firma](https://github.com/ctt-gob-es/clienteafirma), que se carga desde GitHub cuando se inicia la firma; se necesita conexión a Internet y un contexto seguro (HTTPS o localhost).

PINs iniciales:

| Departamento | PINs |
|---|---|
| Limpieza | `4101`–`4110` |
| Conserjería | `5201`–`5210` |
| Jardinería | `6301`–`6310` |

Los nombres iniciales son marcadores editables. Desde **Operarios y PINs**, el administrador puede ver los 30 PINs y actualizar el nombre y apellido de cada persona. Los cambios quedan en SQLite y se reflejan en las sesiones nuevas y en el panel de operarios activos. El inicio de sesión limita los intentos fallidos por dirección IP.

**Seguridad:** no publiques esta lista de PINs. El PIN administrativo solicitado `7804` es una credencial fija en esta versión. Protege el acceso al archivo `Almacenamiento/control_horario.sqlite3`, ya que la pantalla de gestión debe poder mostrar los PINs. El límite de intentos reduce la adivinación, pero un PIN de cuatro dígitos no sustituye a una contraseña robusta.

La aplicación dirige al personal a las vistas según su rol. Las rutas y las API comprueban la sesión y el rol en el servidor. El login por PIN compara exactamente el nombre mostrado y el PIN asignado; no acepta el nombre de un departamento como identidad ni el inicio de sesión heredado por contraseña.

El servidor escucha por defecto en `0.0.0.0:3000` (o en el puerto indicado por la variable de entorno `PORT`). Las rutas públicas `/ping` y `/health` responden con el estado online sin consultar la base de datos. En Render, configura el comando de inicio `python server.py serve`, `ATTENDANCE_SECURE_COOKIE=1` para que las cookies de sesión permitan el acceso HTTPS entre dominios, y `ATTENDANCE_CORS_ORIGINS` con los orígenes frontend separados por comas si son distintos de `https://serviciosgrupoada.es`, `https://www.serviciosgrupoada.es` o `https://stalwart-pudding-467a32.netlify.app`. El frontend administrativo usa la URL pública de Render directamente y requiere que su origen esté permitido por CORS. También puedes especificar `python .\server.py serve --host 0.0.0.0 --port 8000` en un entorno local. Limita el acceso al servidor a redes privadas de confianza: la cuenta administrativa usa un PIN fijo y las cookies no tienen HTTPS en la configuración HTTP local. No expongas el servidor HTTP directamente a Internet; usa un proxy inverso con HTTPS. La geolocalización del navegador requiere HTTPS fuera de `localhost`, aunque el fichaje permite continuar sin coordenadas si no se puede obtener GPS.

La base de datos se crea en `Almacenamiento/control_horario.sqlite3`. Los fichajes guardan el identificador, nombre y departamento del operario, el tipo de evento, la hora UTC recibida por el servidor y la ubicación GPS cuando está disponible con una precisión de hasta 100 metros. El navegador solicita alta precisión, espera hasta 10 segundos y no reutiliza posiciones antiguas; si no obtiene una ubicación suficientemente precisa, permite registrar el fichaje sin coordenadas y muestra un aviso. Cada inicio y cierre de jornada actualiza el JSON estructurado y el informe imprimible del trabajador en `Almacenamiento/control de fichaje`; ambos incluyen fecha, hora de entrada y hora de salida cuando ya está registrada. Los informes ya no se muestran en **Archivo e Impresión**; allí se mantiene el archivo documental. Administración puede corregir nombre, fecha y horas desde **Control horario**; la edición actualiza la base de datos y los archivos. El panel muestra las entradas activas del día con ubicación GPS disponible y su mapa actualiza los marcadores cada diez segundos. No se registran ubicaciones en segundo plano.

La sección **Control mensual de gastos e ingresos** calcula los totales a medida que se introducen las cantidades y solo contacta con la API al pulsar **Guardar / Registrar mes**. El servidor valida y vuelve a calcular los totales y el beneficio, y guarda un registro JSON por mes en `Almacenamiento/control mensual de gastos e ingresos`; si el backend no está disponible, la interfaz conserva los datos y muestra un aviso discreto sin bloquearla. Los registros guardados también se muestran en **Archivo e Impresión**.

## Validación

```powershell
python -m unittest discover -s tests -v
```
