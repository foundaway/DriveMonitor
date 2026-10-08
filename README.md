# DriveMonitor

Aplicación de escritorio para Windows que **monitorea drives Allen-Bradley Kinetix 5500**
leyendo su servidor web integrado y **guarda el historial** (fallas, señales, encoder,
red, información del drive).

> **Solo lectura.** La aplicación solo hace peticiones HTTP **GET**. Cualquier otro
> método está bloqueado en el código. Además, nunca consulta URLs con parámetros
> (`?...`) ni páginas cuyo nombre sugiera una acción (clear, reset, set, write,
> reboot…). Hace una sola petición a la vez por drive, con timeouts cortos y espera
> creciente si el drive no responde.

> **Estado:** probada contra un drive *simulado*, todavía no contra un Kinetix 5500
> real. Las URLs de cada página no están fijas en el código: la app las busca en el
> menú del drive por su nombre. Ver [Primera conexión](#primera-conexión-a-un-drive-real).

---

## Funciones

- **Varios drives a la vez**: se agregan solo con la IP (y un nombre opcional). Cada
  drive tiene su propio hilo de consulta. La lista se guarda entre sesiones.
- **Captura periódica** con intervalos configurables:

  | Página | Intervalo inicial | Cómo se guarda |
  |---|---|---|
  | Monitor Signals | 2 s | todas las lecturas |
  | Fault Log | 15 s | cada falla una sola vez |
  | Encoder Diagnostics | 30 s | todas las lecturas |
  | Ethernet / Network Statistics | 60 s | todas las lecturas |
  | Home, Drive Information, Motor Diagnostics, Network Settings | al conectar y cada 10 min | solo cuando algo cambia |

- **Fault Log**: cada falla se guarda una sola vez. Se deduplica por
  CumulativeUptime + FaultId + FaultSubCode. La hora se guarda en UTC (CipTime GMT del
  drive) y se muestra en hora local (America/Mexico_City). El texto de la falla se
  guarda tal como lo da el drive.
- **Todos los campos** de cada página se guardan en formato genérico clave/valor con
  fecha y hora, aunque la app no los conozca.
- **Eventos**: pérdida y recuperación de comunicación, reinicio del drive (cuando
  baja el Uptime), cambios de firmware y de configuración de red, y cualquier otro
  cambio en la información del drive.
- **Alertas** visuales y notificación de Windows cuando:
  - aparece una falla nueva;
  - el RSSI o el Quality del encoder bajan de 100 %;
  - se incrementa un contador de errores de red (CRC, colisiones, paquetes
    perdidos, descartes, …);
  - se pierde la comunicación o se detecta un reinicio.
- **Interfaz** con tema oscuro:
  - **Vista general**: una tarjeta por drive con su estado de conexión, la última
    falla, las fallas de las últimas 24 h y el RSSI y Quality del encoder.
  - **Detalle del drive**, con estas pestañas:
    - **Fallas**: tabla con filtros (rango de fechas, código, búsqueda), Pareto por
      código, fallas por hora y por turno, y tiempo entre fallas. Al seleccionar una
      falla se ven las señales registradas desde **60 s antes hasta 60 s después**.
    - **Tendencias**: Monitor Signals y encoder (temperatura, voltaje, RSSI, Quality),
      con zoom, selección de rango de fechas y actualización en vivo.
    - **Red**: los contadores de Ethernet y Network como **incremento por intervalo**,
      además del valor acumulado.
    - **Info**: datos del drive, del motor y de la red, con su historial de cambios.
    - **Eventos**: lista de eventos y errores de lectura.
- **Exportar** cualquier tabla o rango de tendencia a **CSV o Excel**.
- **Robusta**: si una página cambia de formato o falla su lectura, el error queda
  registrado (pestaña Eventos y log) y la app sigue con las demás páginas.
- **Log** de la aplicación a archivo, con rotación (5 archivos de 5 MB).

## Instalación (desde el código)

Requisitos: **Windows 10/11** y **Python 3.11 o más reciente**
([python.org](https://www.python.org/downloads/); marcar "Add python.exe to PATH").

```bat
git clone https://github.com/foundaway/DriveMonitor.git
cd DriveMonitor
python -m pip install -r requirements.txt
python run_drivemonitor.py
```

## Generar el .exe

```bat
build.bat
```

El script instala las dependencias, corre las pruebas y genera
**`dist\DriveMonitor\DriveMonitor.exe`**. Para usarlo en otra PC, copia la carpeta
`dist\DriveMonitor` completa; no hace falta instalar Python.

Es equivalente a:

```bat
python -m pip install -r requirements-dev.txt
python -m PyInstaller --noconfirm --clean --windowed --name DriveMonitor ^
  --exclude-module pyqtgraph.examples --collect-data tzdata --hidden-import tzdata ^
  run_drivemonitor.py
```

## Uso

1. **Agregar drive**: escribe la IP (p. ej. `172.23.22.95`) y, si quieres, un nombre.
   La consulta empieza de inmediato.
2. En la **Vista general**, la tarjeta muestra el estado de cada drive. Si tiene una
   alerta activa (sin comunicación, encoder bajo de 100 % o falla en la última hora),
   el borde se pone en rojo.
3. **Abrir detalle** (o doble clic en la tarjeta) abre las pestañas del drive.
4. **Iniciar / Detener** controla cada drive. Los drives detenidos siguen detenidos
   al volver a abrir la app.
5. **Configuración** permite cambiar:
   - los intervalos de consulta (mínimo 2 s);
   - el timeout y la pausa entre peticiones;
   - los turnos (hora de inicio de cada uno);
   - cuántos días guardar Monitor Signals;
   - las notificaciones;
   - la URL manual de una página (ver abajo).
6. Las alertas aparecen en el panel **Alertas** (doble clic abre el drive) y como
   notificación de Windows.

### Dónde se guardan los datos

`%LOCALAPPDATA%\DriveMonitor\`:

- `drivemonitor.db`: historial (SQLite)
- `settings.json`: configuración
- `logs\drivemonitor.log`: log con rotación

Se puede cambiar con la variable de entorno `DRIVEMONITOR_HOME`.

## Primera conexión a un drive real

Al conectar, la app recorre la página principal del drive, sus frames y sus scripts
(solo GET, máximo 40 peticiones). Busca los enlaces del menú por nombre: *Home,
Drive Information, Motor Diagnostics, Encoder Diagnostics, Network Settings,
Ethernet Statistics, Network Statistics, Monitor Signals, Fault Log*. Si una página
carga sus datos con JavaScript, usa el recurso de datos al que hace referencia
(XML, JSON, etc.).

El resultado queda en **Detalle → Eventos → "Páginas encontradas: N de 9"**, con
la lista de enlaces del menú. Si falta alguna página:

- revisa el detalle de ese evento, que muestra el menú que encontró la app;
- en **Configuración → URL manual** asigna la ruta correcta para ese drive;
- o captura las páginas con `tools\capture_drive.py` (ver [`tools/README.md`](tools/README.md))
  y compártelas para ajustar los parsers.

## Pruebas

```bat
python -m pip install -r requirements-dev.txt
python -m pytest
```

- `tests/test_parsers.py`: valores, filas, clave/valor y Fault Log (con los ejemplos
  reales del drive). Si existe `tests/fixtures/raw/manifest.json` (captura del paso 1),
  también procesa cada página real capturada.
- `tests/test_core.py`: seguridad del cliente HTTP (solo GET, URLs bloqueadas),
  emparejado del menú, base de datos, análisis y exportación.
- `tests/test_poller.py`: integración contra un **drive simulado**
  (`tests/mock_drive.py`). Cubre descubrimiento, deduplicación de fallas, alertas,
  reinicio, cambios de firmware y red, errores de red, pérdida y recuperación de
  comunicación, y verifica que **solo se hagan GET** y que nunca se toque "Clear
  Fault Log".

Para probar la interfaz sin un drive real: `python tests\mock_drive.py 8080` y agrega
el drive `127.0.0.1:8080`.

## Estructura

```
drivemonitor/
  http_client.py    cliente HTTP de solo lectura (GET, una conexión, timeouts)
  discovery.py      busca la URL de cada página en el menú del drive
  parsers/          extracción genérica (HTML/XML/JSON/texto), clave/valor, Fault Log
  db.py             historial SQLite
  poller.py         ciclo de consulta por drive, eventos y alertas
  analysis.py       Pareto, por hora/turno, tiempo entre fallas, incrementos
  export.py         CSV y Excel
  config.py         páginas, intervalos y settings.json
  ui/               interfaz PyQt6 + pyqtgraph
tools/capture_drive.py   visor/captura de descubrimiento del paso 1 (solo stdlib)
tests/                   pruebas (pytest) y drive simulado
```

## Notas y limitaciones

- La ventana de **±60 s** alrededor de una falla usa la hora de la falla según el
  **reloj del drive** (CipTime), y las señales usan la hora de la PC. Si los relojes
  no coinciden, la ventana se desplaza; la columna "Detectada (PC)" muestra cuándo
  la vio la app.
- Las señales de Monitor Signals se guardan cada 2 s. Por defecto se conservan
  60 días (configurable; 0 = sin límite).
- Las fallas anteriores a la primera conexión se registran sin generar alertas.
