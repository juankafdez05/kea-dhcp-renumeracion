# Kea DHCPv4 · Cambio de pools y renovación de concesiones

Documentación y herramienta de administración para cambiar un pool
IPv4 en una configuración local de Kea.

El proyecto explica por qué cambiar el rango disponible no equivale
a ordenar a un cliente que abandone inmediatamente su dirección actual.

> **Estado:** herramienta propuesta para pruebas y revisión.
> No se ha validado en todas las versiones o configuraciones de Kea.
> Prueba primero en laboratorio.

## Recursos

- [Guía web](https://juankafdez05.github.io/kea-dhcp-renumeracion/)
- [Código del script](./kea-pool-manager.py)
- [Descarga desde la web](https://juankafdez05.github.io/kea-dhcp-renumeracion/kea-pool-manager.py)

Los enlaces de la web estarán disponibles después de publicar GitHub Pages.

## El problema

Ejemplo:

```text
Subred:       172.21.0.0/24
Pool antiguo: 172.21.0.10 - 172.21.0.30
Pool nuevo:   172.21.0.40 - 172.21.0.70
IP actual:    172.21.0.20
```

El administrador cambia el pool y espera que el cliente reciba otra IP
en su siguiente renovación.

Sin embargo, una renovación en T1 intenta conservar la concesión actual.
No es necesariamente una solicitud de una dirección diferente.

Hay que distinguir:

1. El pool configurado.
2. La concesión registrada en el servidor.
3. El estado y los temporizadores del cliente.
4. La configuración que tiene cargada el proceso de Kea.

La respuesta concreta a una renovación debe comprobarse con la versión,
configuración y tráfico del entorno.

## Qué hace el script

`kea-pool-manager.py` permite:

- Listar subredes y pools.
- Seleccionar una subred mediante `--subnet-id`.
- Seleccionar un pool mediante `--pool-index`.
- Proponer un rango nuevo sin escribir cambios.
- Validar una configuración candidata con `kea-dhcp4 -t`.
- Guardar el cambio con `--write`.
- Crear una copia de seguridad antes de sustituir el archivo.
- Conservar el formato y los comentarios admitidos por su analizador.
- Modificar únicamente el literal del pool seleccionado.
- Detectar cambios en el archivo durante la operación.
- Evitar ejecuciones simultáneas de esta herramienta sobre el mismo archivo.

## Qué NO hace

- No fuerza DHCPNAK en T1 o T2.
- No instala bibliotecas de hooks.
- No borra concesiones.
- No modifica reservas ni temporizadores.
- No recarga ni reinicia Kea.
- No verifica por sí mismo qué archivo utiliza el proceso activo.
- No comprueba si las direcciones nuevas están libres en la red.
- No administra otros productos DHCP.

**Cambiar el pool y forzar una renumeración son tareas diferentes.**

## Alcance y limitaciones

Esta versión está orientada a Linux y a configuraciones locales
autocontenidas de Kea DHCPv4.

Admite subredes directas y subredes dentro de redes compartidas.
Conserva los demás atributos del pool seleccionado.

Se detiene ante:

- Directivas de inclusión `<?include ...?>`.
- Un bloque `config-control` no vacío.
- La biblioteca HA identificada como `libdhcp_ha.so`.
- Bibliotecas cuyo nombre contenga `t1-renumber`.
- Claves JSON duplicadas.
- Sintaxis que su analizador no admita.
- Un rango fuera de la subred seleccionada.
- Un solapamiento con otro pool de esa misma subred.

Estas comprobaciones no sustituyen una revisión de arquitectura:
pueden existir otros mecanismos externos de gestión, sincronización
o alta disponibilidad que el script no detecte.

No utilices esta herramienta para editar directamente una configuración
generada por un appliance, un sistema de automatización o un backend.

## Requisitos

- Linux.
- Python 3.8 o posterior.
- Binario `kea-dhcp4` correspondiente a la instalación administrada.
- Permisos para leer y validar la configuración.
- Permisos de root para utilizar `--write`.
- Directorio de configuración propiedad de root y no escribible
  por el grupo ni por otros usuarios.

No necesita paquetes Python externos.

## Instalación

Descarga o clona el repositorio y revisa el código antes de ejecutarlo.

Desde la carpeta del proyecto:

```bash
python3 -m py_compile kea-pool-manager.py

sudo install -o root -g root -m 0755 \
  kea-pool-manager.py \
  /usr/local/sbin/kea-pool-manager
```

La comprobación con `py_compile` verifica sintaxis Python.
No constituye una prueba funcional del script o de Kea.

## Uso

### Consultar la ayuda

```bash
kea-pool-manager --help
```

### Listar subredes y pools

```bash
sudo kea-pool-manager \
  --config /etc/kea/kea-dhcp4.conf \
  --list
```

Ejemplo:

```text
Subred id=1 172.21.0.0/24 [directa]
  pool-index=0: 172.21.0.10 - 172.21.0.30
```

Los índices de pool empiezan en cero.

### Previsualizar y validar un cambio

```bash
sudo kea-pool-manager \
  --config /etc/kea/kea-dhcp4.conf \
  --subnet-id 1 \
  --pool-index 0 \
  --range "172.21.0.40 - 172.21.0.70"
```

Sin `--write`, el archivo original no se modifica.

### Guardar el cambio

```bash
sudo kea-pool-manager \
  --config /etc/kea/kea-dhcp4.conf \
  --subnet-id 1 \
  --pool-index 0 \
  --range "172.21.0.40 - 172.21.0.70" \
  --write
```

El script solicita confirmación y muestra la ruta de la copia de seguridad.

### Ejecución sin preguntas

```bash
sudo kea-pool-manager \
  --config /etc/kea/kea-dhcp4.conf \
  --subnet-id 1 \
  --pool-index 0 \
  --range "172.21.0.40 - 172.21.0.70" \
  --write \
  --yes \
  --no-color
```

### Indicar otro binario de Kea

Añade, por ejemplo:

```text
--kea-bin /opt/kea/sbin/kea-dhcp4
```

Debe ser el binario correspondiente al servidor que administras.

## Aplicar la configuración al servidor

El script guarda el archivo, pero no lo carga en el proceso activo.

Antes de aplicar el cambio:

1. Confirma el binario y archivo utilizados por el servicio.
2. Revisa las reservas, direcciones estáticas y disponibilidad del rango.
3. Prepara acceso de recuperación.
4. Utiliza una ventana de mantenimiento.

Ejemplo para una instalación con systemd:

```bash
sudo systemctl cat kea-dhcp4-server
sudo systemctl restart kea-dhcp4-server
sudo systemctl status kea-dhcp4-server --no-pager -l
sudo journalctl -u kea-dhcp4-server \
  --since "5 minutes ago" --no-pager
```

Sustituye `kea-dhcp4-server` por la unidad real de tu instalación.

El reinicio puede interrumpir temporalmente el servicio.
Una validación correcta con `-t` no demuestra que el proceso activo
haya cargado el cambio ni que la migración de clientes funcione.

## Verificación con un cliente

Ejemplo de captura en Linux:

```bash
sudo tcpdump -ni any -s0 -vvv \
  'udp port 67 or udp port 68'
```

Comprueba:

- Qué servidor responde.
- Si el cliente está renovando o iniciando una adquisición nueva.
- La dirección confirmada mediante DHCPACK.
- Máscara, puerta de enlace y DNS.
- Conectividad después del cambio.

No ejecutes una liberación de IP desde una sesión remota sin
disponer de acceso alternativo.

## Reversión

El script muestra la ruta de la copia de seguridad creada.

Si necesitas revertir:

1. Identifica la copia correspondiente.
2. Restaura su contenido en el archivo administrado, preservando
   los permisos y propietario requeridos.
3. Valida con el binario correcto de Kea.
4. Aplica la configuración mediante el gestor del servicio.
5. Revisa logs y clientes.

Restaurar el archivo no revierte automáticamente las concesiones
que se hayan entregado mientras estuvo activo el nuevo pool.

## Seguridad

No subas al repositorio:

- Configuraciones reales con contraseñas.
- Tokens o claves.
- Bases de datos de concesiones.
- Copias de seguridad de producción.
- Capturas de tráfico sin anonimizar.
- Identificadores de clientes o información privada de la red.

Las copias de seguridad también pueden contener secretos.

## Publicación web

La página es estática. No se conecta a tu servidor ni ejecuta Python.

El script se publica como archivo descargable y debe revisarse
y ejecutarse localmente por un administrador autorizado.

Estructura:

```text
.
├── index.html
├── README.md
└── kea-pool-manager.py
```

## Referencias

- [RFC 2131 — DHCP](https://www.rfc-editor.org/rfc/rfc2131.html)
- [Kea ARM — Servidor DHCPv4](https://kea.readthedocs.io/en/stable/arm/dhcp4-srv.html)
- [Kea — Configuración](https://kea.readthedocs.io/en/stable/arm/config.html)
- [Kea — API de hooks DHCPv4](https://reports.kea.isc.org/dev_guide/de/df3/dhcpv4Hooks.html)

Consulta siempre la documentación correspondiente a tu versión de Kea.
