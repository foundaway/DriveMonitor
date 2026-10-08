# Visor en vivo y captura del Kinetix 5500 (Paso 1)

`capture_drive.py` es un programa de **solo lectura**: solo hace peticiones HTTP GET,
de una en una, con pausa entre ellas. No envía formularios ni consulta enlaces con
parámetros (`?...`) o con nombres de acción (clear, reset, set, write, …).

## Uso

Doble clic en `capture_drive.exe` (o `python tools\capture_drive.py`). Se abre una ventana:

1. Escribe la **IP del drive** y presiona **Conectar**.
2. El programa busca las páginas del drive y las muestra a la izquierda.
3. Después las consulta en vivo, una por una. Al seleccionar una página se ven sus
   datos a la derecha (pestaña **Datos**) o la respuesta original (pestaña **Crudo**).
   Las filas que cambiaron desde la lectura anterior se resaltan en ámbar.
4. **Refresco por página** controla cada cuántos segundos se vuelve a leer cada
   página (mínimo 2 s).
5. **Detener** corta la comunicación. Si el drive deja de responder, el programa
   lo indica en rojo y reintenta con espera creciente (hasta cada 60 s).

Todo queda guardado en la carpeta **Guardar en** (por defecto `tests\fixtures\raw`
junto al .exe):

- `files\` respuesta original de cada página encontrada
- `manifest.json` lista de páginas, encabezados, enlaces omitidos y errores
- `live\` una copia de cada página cada vez que su contenido cambia (hasta 50 por página)

Modo consola (sin ventana): `capture_drive.exe 172.23.22.95`

## Generar el .exe

```
pip install pyinstaller
pyinstaller --onefile --windowed tools\capture_drive.py
```

El ejecutable queda en `dist\capture_drive.exe`.

## Pruebas

```
python -m unittest discover -s tests
```
