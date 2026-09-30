#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
kea-pool-manager.py
Administrador de pools locales de Kea DHCPv4.

Requisitos:
    Linux, Python >= 3.8 y el binario kea-dhcp4 correspondiente.

Comportamiento:
    --list   Lista subredes y pools.
    Sin --write:
             Muestra y valida el cambio sin modificar el original.
    --write  Crea una copia de seguridad y guarda el cambio.
             NO recarga ni reinicia Kea.

Limitaciones deliberadas:
    - Solo configuraciones locales autocontenidas.
    - No procesa directivas include.
    - No administra configuración mediante backend ni HA.
    - No modifica leases, reservas, temporizadores ni hooks.
    - No fuerza DHCPNAK ni renumeración en T1.
    - No comprueba si las direcciones están ocupadas en la red.
    - Admite JSON con comentarios #, // y /* */.
    - No admite otras extensiones JSON, como comas finales.

La sustitución conserva el contenido del archivo salvo el literal
del pool seleccionado. Antes de escribir, comprueba propietario,
permisos, cambios concurrentes y atributos extendidos.

El bloqueo coordina ejecuciones de ESTA herramienta, no otros editores.
"""

import argparse
from contextlib import contextmanager
import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


VERSION = "1.0.0"
MAX_CONFIG_BYTES = 16 * 1024 * 1024
USE_COLOR = False


class ManagerError(Exception):
    """Error controlado de la herramienta."""


def fail(message):
    raise ManagerError(message)


def log(level, message):
    colors = {
        "INFO": "36",
        "OK": "32",
        "AVISO": "33",
        "ERROR": "31",
    }
    prefix = "[{}]".format(level)

    if USE_COLOR:
        prefix = "\033[{}m{}\033[0m".format(
            colors.get(level, "0"), prefix
        )

    print("{} {}".format(prefix, message), file=sys.stderr)


def banner():
    log("INFO", "Kea Pool Manager v{}".format(VERSION))
    log("INFO", "Administración de pools; sin reinicio automático.")


def object_without_duplicates(pairs):
    result = {}

    for key, value in pairs:
        if key in result:
            fail("Clave JSON duplicada: {!r}".format(key))
        result[key] = value

    return result


def invalid_constant(value):
    fail("Constante no válida en JSON: {}".format(value))


DECODER = json.JSONDecoder(
    object_pairs_hook=object_without_duplicates,
    parse_constant=invalid_constant,
)


def remove_comments(text):
    """
    Sustituye comentarios por espacios conservando offsets y saltos.
    Las cadenas JSON se dejan intactas.

    Rechaza directivas <?...?> fuera de comentarios y cadenas.
    """
    result = list(text)
    position = 0
    length = len(text)

    while position < length:
        char = text[position]

        if char == '"':
            # raw_decode valida escapes y determina el final de la cadena.
            _, position = DECODER.raw_decode(text, position)
            continue

        if text.startswith("<?", position):
            fail(
                "Se han detectado directivas include u otra sintaxis "
                "extendida. Esta versión no modifica esos archivos."
            )

        if char == "#" or text.startswith("//", position):
            end = position

            while end < length and text[end] not in "\r\n":
                end += 1

        elif text.startswith("/*", position):
            closing = text.find("*/", position + 2)

            if closing == -1:
                fail("Comentario de bloque sin cerrar.")

            end = closing + 2

        else:
            position += 1
            continue

        for index in range(position, end):
            if text[index] not in "\r\n":
                result[index] = " "

        position = end

    return "".join(result)


def parse_document(text):
    clean = remove_comments(text)

    data = json.loads(
        clean,
        object_pairs_hook=object_without_duplicates,
        parse_constant=invalid_constant,
    )

    if not isinstance(data, dict):
        fail("La raíz de la configuración debe ser un objeto.")

    if not isinstance(data.get("Dhcp4"), dict):
        fail("No se ha encontrado un objeto Dhcp4 válido.")

    return clean, data


def skip_space(text, position):
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def locate_value(text, path, position=0):
    """
    Localiza el intervalo de caracteres de un valor mediante su ruta.
    Se utiliza sobre JSON ya validado, con comentarios neutralizados.
    """
    position = skip_space(text, position)

    if not path:
        _, end = DECODER.raw_decode(text, position)
        return position, end

    wanted = path[0]

    if isinstance(wanted, str):
        if text[position] != "{":
            fail("No se encontró el objeto JSON esperado.")

        position = skip_space(text, position + 1)

        while text[position] != "}":
            key, end = DECODER.raw_decode(text, position)
            position = skip_space(text, end)

            if text[position] != ":":
                fail("Objeto JSON no válido.")

            position = skip_space(text, position + 1)

            if key == wanted:
                return locate_value(text, path[1:], position)

            _, position = DECODER.raw_decode(text, position)
            position = skip_space(text, position)

            if text[position] != ",":
                break

            position = skip_space(text, position + 1)

    else:
        if text[position] != "[":
            fail("No se encontró la lista JSON esperada.")

        position = skip_space(text, position + 1)
        index = 0

        while text[position] != "]":
            if index == wanted:
                return locate_value(text, path[1:], position)

            _, position = DECODER.raw_decode(text, position)
            position = skip_space(text, position)
            index += 1

            if text[position] != ",":
                break

            position = skip_space(text, position + 1)

    fail("No se pudo localizar el valor seleccionado.")


def get_list(container, key):
    value = container.get(key, [])

    if not isinstance(value, list):
        fail("'{}' debe ser una lista.".format(key))

    return value


def collect_subnets(dhcp4):
    result = []

    def add_subnets(container, base_path, network_name):
        for index, subnet in enumerate(get_list(container, "subnet4")):
            if not isinstance(subnet, dict):
                fail("Cada entrada de subnet4 debe ser un objeto.")

            result.append((
                subnet,
                base_path + ("subnet4", index),
                network_name,
            ))

    add_subnets(dhcp4, ("Dhcp4",), "directa")

    for index, shared in enumerate(get_list(dhcp4, "shared-networks")):
        if not isinstance(shared, dict):
            fail("Cada shared-network debe ser un objeto.")

        add_subnets(
            shared,
            ("Dhcp4", "shared-networks", index),
            str(shared.get("name", "?")),
        )

    return result


def get_pools(subnet):
    pools = get_list(subnet, "pools")

    for pool in pools:
        if not isinstance(pool, dict):
            fail("Cada pool debe ser un objeto.")

        if not isinstance(pool.get("pool"), str):
            fail("Cada pool debe contener un atributo 'pool' de texto.")

    return pools


def list_subnets(subnets):
    if not subnets:
        log("AVISO", "No hay subredes IPv4 en este archivo.")
        return

    for subnet, _, shared_name in subnets:
        print(
            "\nSubred id={} {} [{}]".format(
                subnet.get("id", "?"),
                subnet.get("subnet", "?"),
                shared_name,
            )
        )

        pools = get_pools(subnet)

        if not pools:
            print("  Sin pools.")

        for index, pool in enumerate(pools):
            print("  pool-index={}: {}".format(index, pool["pool"]))


def check_supported(dhcp4):
    if dhcp4.get("config-control"):
        fail(
            "Se ha detectado config-control. Modifica la fuente "
            "de configuración correspondiente, no este archivo."
        )

    for hook in get_list(dhcp4, "hooks-libraries"):
        if not isinstance(hook, dict):
            fail("Cada entrada de hooks-libraries debe ser un objeto.")

        library = hook.get("library", "")

        if not isinstance(library, str):
            fail("El atributo library de un hook debe ser texto.")

        name = Path(library).name.lower()

        if name.startswith("libdhcp_ha.so"):
            fail(
                "Se ha detectado el hook HA. "
                "El cambio requiere un procedimiento coordinado."
            )

        if "t1-renumber" in name:
            fail(
                "Se ha detectado el hook t1-renumber anterior. "
                "No se combinará automáticamente con esta herramienta. "
                "Revisa su retirada y descarga antes de continuar."
            )


def parse_range(value):
    value = value.strip()

    try:
        if "-" in value:
            parts = value.split("-")

            if len(parts) != 2:
                fail("Formato de rango no válido.")

            first = ipaddress.IPv4Address(parts[0].strip())
            last = ipaddress.IPv4Address(parts[1].strip())

        elif "/" in value:
            network = ipaddress.IPv4Network(value, strict=False)
            first = network.network_address
            last = network.broadcast_address

        else:
            first = last = ipaddress.IPv4Address(value)

    except ValueError as error:
        fail("Rango IPv4 no válido: {} ({})".format(value, error))

    if int(first) > int(last):
        fail("El inicio del rango es mayor que el final.")

    return first, last


def prepare_change(clean, text, dhcp4, subnets, args):
    matches = [
        (subnet, path)
        for subnet, path, _ in subnets
        if type(subnet.get("id")) is int
        and subnet["id"] == args.subnet_id
    ]

    if len(matches) != 1:
        fail("El subnet-id no existe o no es único.")

    subnet, subnet_path = matches[0]
    pools = get_pools(subnet)

    index = args.pool_index

    if index is None:
        if len(pools) != 1:
            fail(
                "La subred no tiene exactamente un pool. "
                "Utiliza --list e indica --pool-index."
            )
        index = 0

    if index < 0 or index >= len(pools):
        fail("pool-index fuera de rango.")

    try:
        network = ipaddress.IPv4Network(subnet["subnet"], strict=True)
    except (KeyError, ValueError, TypeError) as error:
        fail("Subred IPv4 no válida: {}".format(error))

    first, last = parse_range(args.new_range)

    if first not in network or last not in network:
        fail("El nuevo rango debe pertenecer a la subred existente.")

    if network.prefixlen <= 30:
        if first == network.network_address:
            fail("El rango incluye la dirección de red.")
        if last == network.broadcast_address:
            fail("El rango incluye la dirección de broadcast.")

    for other_index, pool in enumerate(pools):
        if other_index == index:
            continue

        other_first, other_last = parse_range(pool["pool"])

        if max(int(first), int(other_first)) <= min(
            int(last), int(other_last)
        ):
            fail(
                "Solapamiento con pool-index={}. Esta herramienta "
                "no introduce pools solapados.".format(other_index)
            )

    old = pools[index]["pool"]
    new = "{} - {}".format(first, last)

    if parse_range(old) == (first, last):
        log("OK", "El pool ya contiene ese rango. No hay cambios.")
        return None

    path = subnet_path + ("pools", index, "pool")
    start, end = locate_value(clean, path)

    updated_text = text[:start] + json.dumps(new) + text[end:]

    # Comprobación adicional de la sintaxis del documento resultante.
    parse_document(updated_text)

    log("INFO", "Subred: {} / {}".format(args.subnet_id, network))
    log("INFO", "Índice del pool: {}".format(index))
    log("AVISO", "Antes:   {}".format(old))
    log("OK", "Después: {}".format(new))
    log("AVISO", "No se modificarán leases, reservas ni temporizadores.")
    log(
        "AVISO",
        "Revisa direcciones estáticas, reservas, clases, otros "
        "servidores y mecanismos externos de gestión."
    )

    return updated_text.encode("utf-8")


def stat_signature(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
        info.st_gid,
        info.st_mode,
        info.st_nlink,
    )


def read_snapshot(config):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(str(config), flags)

    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())

        if not stat.S_ISREG(before.st_mode):
            fail("La configuración no es un archivo regular.")

        if before.st_size > MAX_CONFIG_BYTES:
            fail("El archivo supera el límite de 16 MiB.")

        data = stream.read(MAX_CONFIG_BYTES + 1)
        after = os.fstat(stream.fileno())

    if len(data) > MAX_CONFIG_BYTES:
        fail("El archivo supera el límite de 16 MiB.")

    if stat_signature(before) != stat_signature(after):
        fail("La configuración cambió mientras se leía. Repite la orden.")

    return data, after


def ensure_unchanged(config, original, original_stat):
    current, current_stat = read_snapshot(config)

    if (
        current != original
        or stat_signature(current_stat) != stat_signature(original_stat)
    ):
        fail(
            "La configuración cambió durante la operación. "
            "No se ha sobrescrito."
        )


def check_write_permissions(config, info=None):
    if os.geteuid() != 0:
        fail("Para --write, ejecuta el script con sudo.")

    # Rechaza ubicaciones controlables por otros usuarios.
    for directory in (config.parent,) + tuple(config.parent.parents):
        directory_stat = directory.stat()

        if (
            directory_stat.st_uid != 0
            or stat.S_IMODE(directory_stat.st_mode) & 0o022
        ):
            fail(
                "Para escribir, el directorio y sus antecesores deben "
                "pertenecer a root y no ser escribibles por grupo u otros. "
                "Ubicación no admitida: {}".format(directory)
            )

    if info is not None:
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            fail(
                "El archivo debe pertenecer a root y no ser "
                "escribible por grupo u otros."
            )

        if info.st_nlink != 1:
            fail(
                "El archivo tiene enlaces duros. "
                "No se sustituirá automáticamente."
            )


@contextmanager
def writer_lock(config):
    lock_path = config.parent / (
        "." + config.name + ".pool-manager.lock"
    )

    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(str(lock_path), flags, 0o600)

    try:
        info = os.fstat(descriptor)

        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            fail("El archivo de bloqueo no tiene condiciones seguras.")

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("Otra ejecución está administrando este archivo.")

        yield

    finally:
        os.close(descriptor)

    # No se elimina el lock: así se mantiene estable su inode.


def create_private_file(directory, prefix, data):
    descriptor, name = tempfile.mkstemp(
        prefix=prefix,
        dir=str(directory),
    )
    path = Path(name)

    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        return path

    except BaseException:
        path.unlink(missing_ok=True)
        raise


def fsync_file(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)

    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(directory):
    descriptor = os.open(
        str(directory),
        os.O_RDONLY | os.O_DIRECTORY,
    )

    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_access_metadata(source, destination, source_stat):
    """
    Conserva propietario, grupo, permisos y atributos extendidos.

    Si no puede conservarlos, cancela antes de sustituir el original.
    No conserva el mtime: el archivo contiene un cambio nuevo.
    """
    os.chown(
        str(destination),
        source_stat.st_uid,
        source_stat.st_gid,
    )
    os.chmod(str(destination), stat.S_IMODE(source_stat.st_mode))

    attributes = {
        name: os.getxattr(str(source), name)
        for name in os.listxattr(str(source))
    }

    for name in os.listxattr(str(destination)):
        if name not in attributes:
            os.removexattr(str(destination), name)

    for name, value in attributes.items():
        os.setxattr(str(destination), name, value)

    destination_stat = destination.stat()

    if (
        destination_stat.st_uid != source_stat.st_uid
        or destination_stat.st_gid != source_stat.st_gid
        or stat.S_IMODE(destination_stat.st_mode)
        != stat.S_IMODE(source_stat.st_mode)
    ):
        fail("No se pudieron conservar los permisos del archivo.")

    copied = {
        name: os.getxattr(str(destination), name)
        for name in os.listxattr(str(destination))
    }

    if copied != attributes:
        fail("No se pudieron conservar los atributos extendidos.")

    fsync_file(destination)


def find_binary(value):
    binary = shutil.which(value)

    if not binary:
        fail(
            "No se encuentra kea-dhcp4. "
            "Indica el binario correcto con --kea-bin."
        )

    return str(Path(binary).resolve(strict=True))


def validate(binary, candidate, cwd, timeout):
    log("INFO", "Binario de validación: {}".format(binary))
    log("INFO", "Validando configuración candidata con -t...")

    # Salida en un temporal privado: puede contener información sensible.
    with tempfile.TemporaryFile(mode="w+b") as output:
        try:
            result = subprocess.run(
                [binary, "-t", str(candidate)],
                cwd=str(cwd),
                stdout=output,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )

        except subprocess.TimeoutExpired:
            fail(
                "La validación superó {} segundos. "
                "El original no se ha modificado.".format(timeout)
            )

        if result.returncode != 0:
            output.seek(0, os.SEEK_END)
            size = output.tell()
            output.seek(max(0, size - 16000))

            diagnostic = output.read().decode(
                "utf-8", errors="replace"
            )

            log(
                "ERROR",
                "Kea rechazó la configuración (código {}).".format(
                    result.returncode
                ),
            )
            print(diagnostic, file=sys.stderr)

            fail(
                "Validación fallida. No se ha modificado el original. "
                "No publiques el diagnóstico sin revisarlo."
            )

    log("OK", "Validación -t superada.")
    log(
        "AVISO",
        "Esto no confirma la configuración del proceso activo "
        "ni el funcionamiento de los clientes."
    )


def confirm_write(assume_yes):
    if assume_yes:
        return

    if not sys.stdin.isatty():
        fail("Sin terminal interactivo debes indicar --yes.")

    try:
        answer = input(
            "\nSe guardará el cambio SIN reiniciar Kea. "
            "¿Continuar? [s/N]: "
        ).strip().lower()
    except EOFError:
        fail("No se pudo leer la confirmación.")

    if answer not in ("s", "si", "sí", "y", "yes"):
        fail("Operación cancelada por el usuario.")


def preview(config, updated, binary, cwd, timeout):
    with tempfile.TemporaryDirectory(
        prefix="kea-pool-preview-"
    ) as directory:
        candidate = create_private_file(
            Path(directory), "candidate-", updated
        )
        validate(binary, candidate, cwd, timeout)

    log("OK", "Vista previa terminada. No se modificó el archivo.")
    log("INFO", "Para guardar, repite la orden añadiendo --write.")


def write_change(
    config, original, original_stat, updated,
    binary, cwd, timeout, assume_yes
):
    candidate = None
    backup = None

    try:
        # La candidata está en el mismo filesystem que el original.
        candidate = create_private_file(
            config.parent,
            ".{}-candidate-".format(config.name),
            updated,
        )

        validate(binary, candidate, cwd, timeout)
        confirm_write(assume_yes)

        ensure_unchanged(config, original, original_stat)
        copy_access_metadata(config, candidate, original_stat)
        ensure_unchanged(config, original, original_stat)

        stamp = datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y%m%dT%H%M%SZ")

        backup = create_private_file(
            config.parent,
            "{}.bak.{}.".format(config.name, stamp),
            original,
        )

        # Confirma la persistencia de la copia antes de sustituir.
        fsync_directory(config.parent)
        log("OK", "Copia de seguridad privada: {}".format(backup))
        log("INFO", "La copia contiene los bytes originales y tiene modo 0600.")

        # Segunda comprobación tras crear la copia.
        ensure_unchanged(config, original, original_stat)

        os.replace(str(candidate), str(config))
        candidate = None

        try:
            fsync_directory(config.parent)
        except OSError as error:
            fail(
                "El archivo YA ha sido sustituido, pero falló la "
                "sincronización del directorio: {}. "
                "Comprueba su contenido antes de continuar. "
                "Copia: {}".format(error, backup)
            )

        log("OK", "Cambio guardado en {}".format(config))
        log(
            "AVISO",
            "Kea NO se ha recargado ni reiniciado. "
            "Aplica el cambio mediante tu gestor del servicio."
        )
        log(
            "INFO",
            "Después revisa logs, una adquisición DHCP y "
            "la conectividad del cliente."
        )

    finally:
        if candidate is not None:
            candidate.unlink(missing_ok=True)


def process(config, args):
    original, original_stat = read_snapshot(config)

    if args.write:
        check_write_permissions(config, original_stat)

    text = original.decode("utf-8")
    clean, data = parse_document(text)
    dhcp4 = data["Dhcp4"]
    subnets = collect_subnets(dhcp4)

    log("INFO", "Archivo: {}".format(config))

    if args.list:
        list_subnets(subnets)
        return

    check_supported(dhcp4)

    updated = prepare_change(
        clean, text, dhcp4, subnets, args
    )

    if updated is None:
        return

    binary = find_binary(args.kea_bin)

    cwd = (
        Path(args.validation_cwd).expanduser().resolve(strict=True)
        if args.validation_cwd
        else config.parent
    )

    if not cwd.is_dir():
        fail("--validation-cwd debe ser un directorio.")

    log("INFO", "Directorio de validación: {}".format(cwd))
    log(
        "AVISO",
        "Si tu configuración usa rutas relativas, comprueba que este "
        "directorio coincide con el utilizado por tu servicio."
    )

    if args.write:
        write_change(
            config, original, original_stat, updated,
            binary, cwd, args.timeout, args.yes,
        )
    else:
        preview(
            config, updated, binary, cwd, args.timeout
        )


def arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Administrador de pools locales de Kea DHCPv4. "
            "Por defecto solo muestra y valida el cambio."
        ),
        epilog=(
            "No modifica leases ni reinicia Kea. "
            "Consulta el README y prueba primero en laboratorio."
        ),
    )

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + VERSION,
    )
    parser.add_argument(
        "-c", "--config",
        required=True,
        help="Archivo local autocontenido de Kea DHCPv4.",
    )
    parser.add_argument(
        "--kea-bin",
        default="kea-dhcp4",
        help="Binario de la instalación administrada.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Lista subredes y pools, sin modificar nada.",
    )
    parser.add_argument(
        "--subnet-id",
        type=int,
        help="ID de la subred que contiene el pool.",
    )
    parser.add_argument(
        "--pool-index",
        type=int,
        help="Índice del pool, empezando en cero.",
    )
    parser.add_argument(
        "--range",
        dest="new_range",
        help="Nuevo rango: IP-IP, IP/CIDR o IP.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Guarda el cambio con copia de seguridad; no reinicia.",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="Autoriza la escritura sin preguntar.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Desactiva los colores.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=90,
        help="Tiempo máximo de validación en segundos (90).",
    )
    parser.add_argument(
        "--validation-cwd",
        help=(
            "Directorio de trabajo para validar rutas relativas. "
            "Por defecto, el directorio de la configuración."
        ),
    )

    args = parser.parse_args()

    if args.timeout < 1:
        parser.error("--timeout debe ser mayor que cero.")

    if args.list:
        if (
            args.write
            or args.subnet_id is not None
            or args.pool_index is not None
            or args.new_range is not None
            or args.yes
        ):
            parser.error("--list no se combina con opciones de modificación.")

    else:
        if args.subnet_id is None or args.new_range is None:
            parser.error("Indica --subnet-id y --range, o utiliza --list.")

        if not 1 <= args.subnet_id <= 4294967294:
            parser.error("--subnet-id debe estar entre 1 y 4294967294.")

        if args.pool_index is not None and args.pool_index < 0:
            parser.error("--pool-index no puede ser negativo.")

        if args.yes and not args.write:
            parser.error("--yes solo tiene sentido junto con --write.")

    return args


def main():
    global USE_COLOR

    if sys.version_info < (3, 8):
        fail("Se requiere Python 3.8 o posterior.")

    if not sys.platform.startswith("linux"):
        fail("Esta versión está diseñada para Linux.")

    args = arguments()

    USE_COLOR = (
        not args.no_color
        and "NO_COLOR" not in os.environ
        and sys.stderr.isatty()
        and os.environ.get("TERM", "") != "dumb"
    )

    banner()

    supplied = Path(args.config).expanduser()

    if supplied.is_symlink():
        fail(
            "La ruta indicada es un enlace simbólico. "
            "Indica explícitamente el archivo real."
        )

    config = supplied.resolve(strict=True)

    if args.write:
        check_write_permissions(config)

        with writer_lock(config):
            process(config, args)

    else:
        process(config, args)


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        log(
            "AVISO",
            "Interrumpido. Si utilizaste --write, comprueba "
            "el archivo y las copias antes de repetir."
        )
        sys.exit(130)

    except json.JSONDecodeError as error:
        log(
            "ERROR",
            "JSON no admitido en línea {}, columna {}: {}. "
            "Se permiten comentarios, pero no comas finales "
            "ni otras extensiones.".format(
                error.lineno, error.colno, error.msg
            ),
        )
        sys.exit(1)

    except UnicodeDecodeError:
        log("ERROR", "El archivo debe utilizar codificación UTF-8.")
        sys.exit(1)

    except (ManagerError, OSError) as error:
        log("ERROR", str(error))
        sys.exit(1)

    except Exception as error:
        log(
            "ERROR",
            "Fallo inesperado ({}): {}".format(
                type(error).__name__, error
            ),
        )
        log(
            "AVISO",
            "Comprueba el archivo y las copias antes de continuar."
        )
        sys.exit(1)
