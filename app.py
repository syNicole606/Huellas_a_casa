from flask import Flask, request, jsonify, render_template
from db import get_connection
import hashlib
import mysql.connector
from datetime import datetime, timedelta

app = Flask(__name__)

# ------------------------------------------------------------
# Utilidades para notificaciones y recordatorios
# ------------------------------------------------------------
SERVICIOS_TEXTO = {
    "consulta_general": "consulta general",
    "vacunacion": "vacunación",
    "desparasitacion": "desparasitación",
    "control": "control"
}


def formatear_hora_12h(valor_hora):
    """Convierte una columna TIME de MySQL (timedelta o string) a formato 12h."""
    if hasattr(valor_hora, "total_seconds"):
        total = int(valor_hora.total_seconds())
        horas = (total // 3600) % 24
        minutos = (total % 3600) // 60
    else:
        partes = str(valor_hora).split(":")
        horas, minutos = int(partes[0]), int(partes[1])

    periodo = "AM" if horas < 12 else "PM"
    horas12 = horas % 12
    if horas12 == 0:
        horas12 = 12
    return f"{horas12}:{minutos:02d} {periodo}"


def formatear_tiempo(fecha_creado):
    """Convierte un datetime en un texto relativo tipo 'Hace 10 min', 'Ayer, 4:30 PM'."""
    ahora = datetime.now()
    diferencia = ahora - fecha_creado
    segundos = diferencia.total_seconds()

    if segundos < 60:
        return "Hace un momento"

    minutos = int(segundos // 60)
    if minutos < 60:
        return f"Hace {minutos} min"

    if fecha_creado.date() == ahora.date():
        horas = minutos // 60
        return f"Hace {horas} h"

    ayer = ahora.date() - timedelta(days=1)
    if fecha_creado.date() == ayer:
        return f"Ayer, {formatear_hora_12h(fecha_creado.time())}"

    dias = (ahora.date() - fecha_creado.date()).days
    return f"Hace {dias} días"


def generar_recordatorios_citas(cursor, conexion, id_usuario):
    """
    Revisa si el usuario tiene citas confirmadas/pendientes para MAÑANA
    y crea la notificación de recordatorio si todavía no existe
    (usa id_referencia para no duplicarla en cada visita).
    """
    cursor.execute("""
        SELECT c.id, c.hora, c.tipo_servicio, m.nombre AS mascota
        FROM citas c
        INNER JOIN mascotas m ON c.id_mascota = m.id
        WHERE c.id_usuario = %s
          AND c.fecha = CURDATE() + INTERVAL 1 DAY
          AND c.estado IN ('confirmada', 'pendiente')
    """, (id_usuario,))
    citas_manana = cursor.fetchall()

    for c in citas_manana:
        cursor.execute("""
            SELECT id FROM notificaciones
            WHERE id_usuario = %s AND tipo = 'cita'
              AND titulo = 'Recordatorio de Cita' AND id_referencia = %s
        """, (id_usuario, c["id"]))

        if cursor.fetchone():
            continue  # ya se generó este recordatorio antes

        servicio = SERVICIOS_TEXTO.get(c["tipo_servicio"], c["tipo_servicio"])
        hora_txt = formatear_hora_12h(c["hora"])
        descripcion = f'Mañana tienes una cita de {servicio} para "{c["mascota"]}" a las {hora_txt}.'

        cursor.execute("""
            INSERT INTO notificaciones (id_usuario, titulo, descripcion, tipo, leida, id_referencia)
            VALUES (%s, 'Recordatorio de Cita', %s, 'cita', 0, %s)
        """, (id_usuario, descripcion, c["id"]))

    if citas_manana:
        conexion.commit()

# ------------------------------------------------------------
# Rutas para SERVIR las páginas HTML
# ------------------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/<page>")
def serve_page(page):
    return render_template(page)


# ------------------------------------------------------------
# API: REGISTRAR USUARIO
# ------------------------------------------------------------
@app.route("/api/registrar", methods=["POST"])
def registrar():
    datos = request.get_json()

    nombre = datos.get("nombre")
    cedula = datos.get("cedula")
    correo = datos.get("correo")
    contrasena = datos.get("contrasena")
    telefono = datos.get("telefono")

    if not nombre or not correo or not contrasena:
        return jsonify({"exito": False, "mensaje": "Faltan datos obligatorios"}), 400

    conexion = get_connection()
    cursor = conexion.cursor()

    try:
        cursor.execute("SELECT id FROM usuarios WHERE correo = %s", (correo,))
        if cursor.fetchone():
            return jsonify({"exito": False, "mensaje": "Este correo ya está registrado"}), 409

        contrasena_hash = hashlib.sha256(contrasena.encode()).hexdigest()

        sql = """
            INSERT INTO usuarios (nombre, cedula, correo, contrasena, telefono, proveedor, acepta_tratamiento, fecha_aceptacion)
            VALUES (%s, %s, %s, %s, %s, 'local', 1, NOW())
        """
        cursor.execute(sql, (nombre, cedula, correo, contrasena_hash, telefono))
        conexion.commit()

        nuevo_id = cursor.lastrowid

        cursor.execute("""
            INSERT INTO notificaciones (id_usuario, titulo, descripcion, tipo, leida)
            VALUES (%s, %s, %s, 'sistema', 0)
        """, (nuevo_id, "¡Bienvenido a Huellas a Casa!",
              "Gracias por registrarte. Agenda tu primera cita con nosotros. 🐾"))
        conexion.commit()

        # Se arma el objeto "usuario" con el mismo formato que devuelve /api/login
        # (id, nombre, correo) para que el frontend pueda iniciar sesión de inmediato
        # tras registrarse, sin pedirle correo/contraseña otra vez.
        usuario = {"id": nuevo_id, "nombre": nombre, "correo": correo}

        return jsonify({
            "exito": True,
            "mensaje": "Usuario registrado correctamente",
            "usuario": usuario
        })

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()
# ------------------------------------------------------------
# API: INICIAR SESIÓN
# ------------------------------------------------------------
# ------------------------------------------------------------
# API: INICIAR SESIÓN  (ÚNICO CAMBIO: se agrega "rol" al SELECT
# y a la respuesta, para que el frontend sepa a qué panel mandar
# al usuario. El resto de la función queda igual.)
# ------------------------------------------------------------
@app.route("/api/login", methods=["POST"])
def login():
    datos = request.get_json()
    correo = datos.get("correo")
    contrasena = datos.get("contrasena")

    if not correo or not contrasena:
        return jsonify({"exito": False, "mensaje": "Faltan datos"}), 400

    contrasena_hash = hashlib.sha256(contrasena.encode()).hexdigest()

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT id, nombre, correo, rol
            FROM usuarios
            WHERE correo = %s AND contrasena = %s AND activo = 1
        """, (correo, contrasena_hash))

        usuario = cursor.fetchone()

        if usuario:
            cursor.execute("UPDATE usuarios SET ultimo_acceso = NOW() WHERE id = %s", (usuario["id"],))
            conexion.commit()

            cursor.execute("""
                INSERT INTO historial_accesos (id_usuario, evento, ip_acceso)
                VALUES (%s, 'login', %s)
            """, (usuario["id"], request.remote_addr))
            conexion.commit()

            return jsonify({"exito": True, "usuario": usuario})
        else:
            return jsonify({"exito": False, "mensaje": "Correo o contraseña incorrectos"}), 401

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()
# ------------------------------------------------------------
# API: AGENDAR CITA
# ------------------------------------------------------------
@app.route("/api/agendar_cita", methods=["POST"])
def agendar_cita():
    datos = request.get_json()

    id_usuario = datos.get("id_usuario")
    nombre_dueno = datos.get("nombre_dueno")
    telefono = datos.get("telefono")
    nombre_mascota = datos.get("nombre_mascota")
    tipo_mascota = datos.get("tipo_mascota")
    tipo_mascota_otro = datos.get("tipo_mascota_otro")
    veterinario = datos.get("veterinario")
    fecha = datos.get("fecha")
    hora = datos.get("hora")
    tipo_servicio = datos.get("tipo_servicio")
    subtipo = datos.get("subtipo")

    if not all([id_usuario, nombre_dueno, telefono, nombre_mascota, tipo_mascota, veterinario, fecha, hora, tipo_servicio]):
        return jsonify({"exito": False, "mensaje": "Faltan datos para agendar la cita"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id FROM mascotas WHERE id_usuario = %s AND nombre = %s",
                        (id_usuario, nombre_mascota))
        mascota = cursor.fetchone()

        if mascota:
            id_mascota = mascota["id"]
        else:
            cursor.execute("""
                INSERT INTO mascotas (id_usuario, nombre, tipo, tipo_otro)
                VALUES (%s, %s, %s, %s)
            """, (id_usuario, nombre_mascota, tipo_mascota, tipo_mascota_otro))
            conexion.commit()
            id_mascota = cursor.lastrowid

        cursor.execute("SELECT id FROM veterinarios WHERE nombre = %s", (veterinario,))
        vet = cursor.fetchone()
        if not vet:
            return jsonify({"exito": False, "mensaje": "Veterinario no encontrado"}), 404
        id_veterinario = vet["id"]

        cursor.execute("""
            INSERT INTO citas (id_usuario, id_mascota, id_veterinario, tipo_servicio, subtipo,
                                nombre_dueno, telefono_dueno, fecha, hora, estado, pago_estado)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmada', 'pendiente')
        """, (id_usuario, id_mascota, id_veterinario, tipo_servicio, subtipo,
              nombre_dueno, telefono, fecha, hora))
        conexion.commit()
        id_cita = cursor.lastrowid

        cursor.execute("""
            INSERT INTO notificaciones (id_usuario, titulo, descripcion, tipo, leida)
            VALUES (%s, %s, %s, 'cita', 0)
        """, (id_usuario, "Cita Confirmada",
              f'Tu cita de {tipo_servicio} para "{nombre_mascota}" ha sido agendada para el {fecha} a las {hora}.'))
        conexion.commit()

        return jsonify({"exito": True, "id_cita": id_cita})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: OBTENER MIS CITAS (próxima + historial)
# ------------------------------------------------------------
@app.route("/api/mis_citas", methods=["GET"])
def mis_citas():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        generar_recordatorios_citas(cursor, conexion, id_usuario)

        cursor.execute("""
            SELECT c.id, c.fecha, c.hora, c.tipo_servicio, c.subtipo, c.estado,
                   c.nombre_dueno, c.telefono_dueno,
                   m.nombre AS mascota, m.tipo AS tipo_mascota,
                   v.nombre AS veterinario
            FROM citas c
            INNER JOIN mascotas m ON c.id_mascota = m.id
            INNER JOIN veterinarios v ON c.id_veterinario = v.id
            WHERE c.id_usuario = %s
              AND c.estado IN ('confirmada', 'pendiente')
              AND c.fecha >= CURDATE()
            ORDER BY c.fecha ASC, c.hora ASC
            LIMIT 1
        """, (id_usuario,))
        proxima = cursor.fetchone()

        if proxima:
            proxima["fecha"] = proxima["fecha"].strftime("%Y-%m-%d")
            proxima["hora"] = str(proxima["hora"])

        cursor.execute("""
            SELECT c.id, c.fecha, c.hora, c.tipo_servicio, c.estado, c.pago_estado,
                   m.nombre AS mascota, m.tipo AS tipo_mascota,
                   v.nombre AS veterinario
            FROM citas c
            INNER JOIN mascotas m ON c.id_mascota = m.id
            INNER JOIN veterinarios v ON c.id_veterinario = v.id
            WHERE c.id_usuario = %s
              AND c.estado IN ('atendida', 'cancelada')
            ORDER BY c.fecha DESC, c.hora DESC
            LIMIT 10
        """, (id_usuario,))
        historial = cursor.fetchall()

        for fila in historial:
            fila["fecha"] = fila["fecha"].strftime("%Y-%m-%d")
            fila["hora"] = str(fila["hora"])

        return jsonify({"exito": True, "proxima": proxima, "historial": historial})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: OBTENER TODAS MIS CITAS (acumulado, cualquier fecha/estado)
# ------------------------------------------------------------
@app.route("/api/todas_citas", methods=["GET"])
def todas_citas():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT c.id, c.fecha, c.hora, c.tipo_servicio, c.subtipo, c.estado, c.pago_estado,
                   m.nombre AS mascota, m.tipo AS tipo_mascota,
                   v.nombre AS veterinario
            FROM citas c
            INNER JOIN mascotas m ON c.id_mascota = m.id
            INNER JOIN veterinarios v ON c.id_veterinario = v.id
            WHERE c.id_usuario = %s
            ORDER BY c.fecha DESC, c.hora DESC
        """, (id_usuario,))
        citas = cursor.fetchall()

        for fila in citas:
            fila["fecha"] = fila["fecha"].strftime("%Y-%m-%d")
            fila["hora"] = str(fila["hora"])

        return jsonify({"exito": True, "citas": citas})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: CANCELAR CITA
# ------------------------------------------------------------
@app.route("/api/cancelar_cita", methods=["POST"])
def cancelar_cita():
    datos = request.get_json()

    id_cita = datos.get("id_cita")
    id_usuario = datos.get("id_usuario")

    if not id_cita or not id_usuario:
        return jsonify({"exito": False, "mensaje": "Faltan datos (id_cita / id_usuario)"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT id, estado
            FROM citas
            WHERE id = %s AND id_usuario = %s
        """, (id_cita, id_usuario))
        cita = cursor.fetchone()

        if not cita:
            return jsonify({"exito": False, "mensaje": "La cita no existe o no te pertenece"}), 404

        if cita["estado"] not in ("pendiente", "confirmada"):
            return jsonify({"exito": False, "mensaje": "Esta cita ya no se puede cancelar"}), 400

        cursor.execute("UPDATE citas SET estado = 'cancelada' WHERE id = %s", (id_cita,))
        conexion.commit()

        cursor.execute("""
            INSERT INTO notificaciones (id_usuario, titulo, descripcion, tipo, leida)
            VALUES (%s, %s, %s, 'cita', 0)
        """, (id_usuario, "Cita Cancelada", "Tu cita ha sido cancelada exitosamente."))
        conexion.commit()

        return jsonify({"exito": True, "mensaje": "Cita cancelada correctamente"})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: FINALIZAR COMPRA (guarda pedido + detalle + notificación)
# ------------------------------------------------------------
@app.route("/api/finalizar_compra", methods=["POST"])
def finalizar_compra():
    datos = request.get_json()

    id_usuario = datos.get("id_usuario")
    nombre_envio = datos.get("nombre")
    correo_envio = datos.get("correo")
    telefono_envio = datos.get("telefono")
    pais = datos.get("pais")
    ciudad = datos.get("ciudad")
    direccion = datos.get("direccion")
    codigo_postal = datos.get("codigo_postal")
    metodo_pago = datos.get("metodo_pago")
    carrito = datos.get("carrito")  # [{id, nombre, precio, qty}, ...]

    if not all([id_usuario, nombre_envio, correo_envio, telefono_envio,
                pais, ciudad, direccion, metodo_pago]) or not carrito:
        return jsonify({"exito": False, "mensaje": "Faltan datos para procesar la compra"}), 400

    COSTO_ENVIO = 8000

    try:
        subtotal = sum(int(item["precio"]) * int(item["qty"]) for item in carrito)
    except (KeyError, TypeError, ValueError):
        return jsonify({"exito": False, "mensaje": "El carrito tiene datos inválidos"}), 400

    total = subtotal + COSTO_ENVIO

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        # 1. Crear el pedido
        cursor.execute("""
            INSERT INTO pedidos (id_usuario, nombre_envio, correo_envio, telefono_envio,
                                  pais, ciudad, direccion, codigo_postal,
                                  metodo_pago, costo_envio, total, estado)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'confirmado')
        """, (id_usuario, nombre_envio, correo_envio, telefono_envio,
              pais, ciudad, direccion, codigo_postal,
              metodo_pago, COSTO_ENVIO, total))
        conexion.commit()
        id_pedido = cursor.lastrowid

        # 2. Insertar cada producto del carrito en detalle_pedido
        for item in carrito:
            cursor.execute("""
                INSERT INTO detalle_pedido (id_pedido, id_producto, cantidad, precio_unidad)
                VALUES (%s, %s, %s, %s)
            """, (id_pedido, item["id"], item["qty"], item["precio"]))
        conexion.commit()

        # 3. Notificación de compra exitosa
        cursor.execute("""
            INSERT INTO notificaciones (id_usuario, titulo, descripcion, tipo, leida)
            VALUES (%s, %s, %s, 'pedido', 0)
        """, (id_usuario, "¡Compra Exitosa!",
              "Tu pedido ha sido procesado correctamente. ¡Gracias por confiar en Huellas a Casa! 🐾"))
        conexion.commit()

        return jsonify({"exito": True, "id_pedido": id_pedido, "total": total})

    except mysql.connector.Error as err:
        conexion.rollback()
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: OBTENER NOTIFICACIONES (genera recordatorios pendientes primero)
# ------------------------------------------------------------
@app.route("/api/notificaciones", methods=["GET"])
def notificaciones():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        # Antes de listar, generamos el recordatorio de "cita mañana" si aplica
        generar_recordatorios_citas(cursor, conexion, id_usuario)

        cursor.execute("""
            SELECT id, titulo, descripcion, tipo, leida, creado_en
            FROM notificaciones
            WHERE id_usuario = %s
            ORDER BY creado_en DESC
        """, (id_usuario,))
        filas = cursor.fetchall()

        resultado = [{
            "id": f["id"],
            "titulo": f["titulo"],
            "descripcion": f["descripcion"],
            "tipo": f["tipo"],
            "leida": bool(f["leida"]),
            "tiempo": formatear_tiempo(f["creado_en"])
        } for f in filas]

        return jsonify({"exito": True, "notificaciones": resultado})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: MARCAR NOTIFICACIÓN COMO LEÍDA
# ------------------------------------------------------------
@app.route("/api/marcar_leida", methods=["POST"])
def marcar_leida():
    datos = request.get_json()
    id_notificacion = datos.get("id_notificacion")
    id_usuario = datos.get("id_usuario")

    if not id_notificacion or not id_usuario:
        return jsonify({"exito": False, "mensaje": "Faltan datos"}), 400

    conexion = get_connection()
    cursor = conexion.cursor()

    try:
        cursor.execute("""
            UPDATE notificaciones
            SET leida = 1
            WHERE id = %s AND id_usuario = %s
        """, (id_notificacion, id_usuario))
        conexion.commit()

        return jsonify({"exito": True})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()
        
        # ============================================================
# NUEVOS ENDPOINTS: PANELES DE ROL
# Agregar todo este bloque a app.py, antes de la sección
# "INICIAR SERVIDOR" al final del archivo.
# Nada de lo existente se modifica.
# ============================================================

# ------------------------------------------------------------
# Utilidad: verificar que un usuario tenga uno de los roles permitidos.
# Se usa al inicio de cada endpoint protegido. Esta validación vive
# en el backend (no basta con ocultar botones en el frontend), porque
# cualquiera puede editar sessionStorage desde la consola del navegador.
# ------------------------------------------------------------
def verificar_rol(cursor, id_usuario, roles_permitidos):
    """Devuelve el usuario (dict con rol incluido) si tiene permiso, o None si no."""
    cursor.execute("SELECT id, rol, activo FROM usuarios WHERE id = %s", (id_usuario,))
    usuario = cursor.fetchone()
    if not usuario or not usuario["activo"]:
        return None
    if usuario["rol"] not in roles_permitidos:
        return None
    return usuario


# ------------------------------------------------------------
# API: CITAS DE UN VETERINARIO (panel de veterinario)
# ------------------------------------------------------------
@app.route("/api/citas_veterinario", methods=["GET"])
def citas_veterinario():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("veterinario", "admin"))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        # Se busca el id de la fila en "veterinarios" vinculada a esta cuenta
        cursor.execute("SELECT id FROM veterinarios WHERE id_usuario = %s", (id_usuario,))
        vet = cursor.fetchone()
        if not vet:
            return jsonify({"exito": False, "mensaje": "Esta cuenta no está vinculada a un veterinario"}), 404

        cursor.execute("""
            SELECT c.id, c.fecha, c.hora, c.tipo_servicio, c.subtipo, c.estado, c.pago_estado,
                   u.nombre AS dueno, u.telefono AS telefono_dueno,
                   m.nombre AS mascota, m.tipo AS tipo_mascota
            FROM citas c
            INNER JOIN usuarios u ON c.id_usuario = u.id
            INNER JOIN mascotas m ON c.id_mascota = m.id
            WHERE c.id_veterinario = %s
              AND c.estado IN ('pendiente', 'confirmada')
              AND c.fecha >= CURDATE()
            ORDER BY c.fecha ASC, c.hora ASC
        """, (vet["id"],))
        citas = cursor.fetchall()

        for fila in citas:
            fila["fecha"] = fila["fecha"].strftime("%Y-%m-%d")
            fila["hora"] = str(fila["hora"])

        return jsonify({"exito": True, "citas": citas})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: MARCAR CITA COMO ATENDIDA (panel de veterinario)
# ------------------------------------------------------------
@app.route("/api/marcar_atendida", methods=["POST"])
def marcar_atendida():
    datos = request.get_json()
    id_cita = datos.get("id_cita")
    id_usuario = datos.get("id_usuario")

    if not id_cita or not id_usuario:
        return jsonify({"exito": False, "mensaje": "Faltan datos"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("veterinario", "admin"))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        # Verificar que la cita pertenezca al veterinario que hace la petición
        cursor.execute("""
            SELECT c.id FROM citas c
            INNER JOIN veterinarios v ON c.id_veterinario = v.id
            WHERE c.id = %s AND v.id_usuario = %s
        """, (id_cita, id_usuario))
        if not cursor.fetchone() and usuario["rol"] != "admin":
            return jsonify({"exito": False, "mensaje": "Esta cita no te pertenece"}), 403

        cursor.execute("UPDATE citas SET estado = 'atendida' WHERE id = %s", (id_cita,))
        conexion.commit()

        return jsonify({"exito": True, "mensaje": "Cita marcada como atendida"})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: CONSULTAR DISPONIBILIDAD (uso público, para agendar citas)
# ------------------------------------------------------------
@app.route("/api/disponibilidad", methods=["GET"])
def obtener_disponibilidad():
    id_veterinario = request.args.get("id_veterinario")
    fecha = request.args.get("fecha")

    if not id_veterinario or not fecha:
        return jsonify({"exito": False, "mensaje": "Faltan datos (id_veterinario / fecha)"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT id, hora, disponible
            FROM disponibilidad_veterinario
            WHERE id_veterinario = %s AND fecha = %s
            ORDER BY hora
        """, (id_veterinario, fecha))
        filas = cursor.fetchall()

        for fila in filas:
            fila["hora"] = str(fila["hora"])

        return jsonify({"exito": True, "disponibilidad": filas})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: REPORTAR DISPONIBILIDAD (panel de veterinario)
# ------------------------------------------------------------
@app.route("/api/reportar_disponibilidad", methods=["POST"])
def reportar_disponibilidad():
    datos = request.get_json()
    id_usuario = datos.get("id_usuario")
    fecha = datos.get("fecha")
    horas = datos.get("horas")  # lista de horas, ej: ["09:00:00", "10:30:00"]

    if not id_usuario or not fecha or not horas:
        return jsonify({"exito": False, "mensaje": "Faltan datos (id_usuario / fecha / horas)"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("veterinario",))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("SELECT id FROM veterinarios WHERE id_usuario = %s", (id_usuario,))
        vet = cursor.fetchone()
        if not vet:
            return jsonify({"exito": False, "mensaje": "Esta cuenta no está vinculada a un veterinario"}), 404

        for hora in horas:
            cursor.execute("""
                INSERT INTO disponibilidad_veterinario (id_veterinario, fecha, hora, disponible)
                VALUES (%s, %s, %s, 1)
                ON DUPLICATE KEY UPDATE disponible = 1
            """, (vet["id"], fecha, hora))
        conexion.commit()

        return jsonify({"exito": True, "mensaje": "Disponibilidad guardada correctamente"})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: CRUD DE PRODUCTOS (panel de tienda)
# ------------------------------------------------------------
@app.route("/api/producto_crear", methods=["POST"])
def producto_crear():
    datos = request.get_json()
    id_usuario = datos.get("id_usuario")
    nombre = datos.get("nombre")
    descripcion = datos.get("descripcion")
    categoria = datos.get("categoria")
    precio = datos.get("precio")
    emoji = datos.get("emoji")

    if not id_usuario or not nombre or precio is None:
        return jsonify({"exito": False, "mensaje": "Faltan datos obligatorios"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin_tienda", "admin"))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("""
            INSERT INTO productos (nombre, descripcion, categoria, precio, emoji, activo)
            VALUES (%s, %s, %s, %s, %s, 1)
        """, (nombre, descripcion, categoria, precio, emoji))
        conexion.commit()

        return jsonify({"exito": True, "id": cursor.lastrowid})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


@app.route("/api/producto_actualizar", methods=["POST"])
def producto_actualizar():
    datos = request.get_json()
    id_usuario = datos.get("id_usuario")
    id_producto = datos.get("id_producto")
    nombre = datos.get("nombre")
    descripcion = datos.get("descripcion")
    categoria = datos.get("categoria")
    precio = datos.get("precio")
    emoji = datos.get("emoji")

    if not id_usuario or not id_producto:
        return jsonify({"exito": False, "mensaje": "Faltan datos obligatorios"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin_tienda", "admin"))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("""
            UPDATE productos
            SET nombre = %s, descripcion = %s, categoria = %s, precio = %s, emoji = %s
            WHERE id = %s
        """, (nombre, descripcion, categoria, precio, emoji, id_producto))
        conexion.commit()

        return jsonify({"exito": True})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# Baja lógica del producto (activo = 0), NUNCA se borra la fila,
# por la misma razón que las citas canceladas no se borran: se
# conserva el historial de pedidos que ya lo referencian.
@app.route("/api/producto_desactivar", methods=["POST"])
def producto_desactivar():
    datos = request.get_json()
    id_usuario = datos.get("id_usuario")
    id_producto = datos.get("id_producto")

    if not id_usuario or not id_producto:
        return jsonify({"exito": False, "mensaje": "Faltan datos obligatorios"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin_tienda", "admin"))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("UPDATE productos SET activo = 0 WHERE id = %s", (id_producto,))
        conexion.commit()

        return jsonify({"exito": True})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: RESUMEN GENERAL (panel de administrador)
# ------------------------------------------------------------
@app.route("/api/resumen_admin", methods=["GET"])
def resumen_admin():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin",))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("SELECT * FROM v_resumen")
        resumen = cursor.fetchone()

        cursor.execute("SELECT * FROM v_citas_servicio")
        citas_servicio = cursor.fetchall()

        cursor.execute("SELECT * FROM v_citas_veterinario")
        citas_veterinario = cursor.fetchall()

        cursor.execute("SELECT * FROM v_top_productos")
        top_productos = cursor.fetchall()

        return jsonify({
            "exito": True,
            "resumen": resumen,
            "citas_por_servicio": citas_servicio,
            "citas_por_veterinario": citas_veterinario,
            "top_productos": top_productos
        })

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()

# ============================================================
# NUEVOS ENDPOINTS: PANEL DE ADMINISTRADOR (paneladmin.html)
# Agregar este bloque a app.py, antes de la sección
# "INICIAR SERVIDOR" al final del archivo, y después del bloque
# de "verificar_rol" que ya agregaste con los endpoints de rol.
# Nada de lo existente se modifica.
# ============================================================

# ------------------------------------------------------------
# API: GESTIÓN DE CITAS (panel de administrador)
# Devuelve TODAS las citas del sistema, no solo las de un
# veterinario. Formato de columnas: id_cita, dueño, mascota,
# veterinario, servicio, fecha, hora, estado
# ------------------------------------------------------------
@app.route("/api/citas_admin", methods=["GET"])
def citas_admin():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin",))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("""
            SELECT c.id AS id_cita,
                   u.nombre AS dueño,
                   m.nombre AS mascota,
                   v.nombre AS veterinario,
                   c.tipo_servicio AS servicio,
                   c.fecha, c.hora, c.estado
            FROM citas c
            INNER JOIN usuarios u ON c.id_usuario = u.id
            INNER JOIN mascotas m ON c.id_mascota = m.id
            INNER JOIN veterinarios v ON c.id_veterinario = v.id
            ORDER BY c.fecha DESC, c.hora DESC
        """)
        citas = cursor.fetchall()

        for fila in citas:
            fila["fecha"] = fila["fecha"].strftime("%Y-%m-%d")
            fila["hora"] = str(fila["hora"])

        return jsonify({"exito": True, "resultados": citas})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: USUARIOS Y ROLES (panel de administrador)
# Formato de columnas: id, nombre, correo, rol
# ------------------------------------------------------------
@app.route("/api/usuarios", methods=["GET"])
def usuarios_admin():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin",))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("""
            SELECT id, nombre, correo, rol
            FROM usuarios
            ORDER BY FIELD(rol, 'admin', 'admin_tienda', 'veterinario', 'cliente'), nombre
        """)
        usuarios = cursor.fetchall()

        return jsonify({"exito": True, "resultados": usuarios})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: CAMBIAR EL ROL DE UN USUARIO (panel de administrador)
# Endpoint extra útil para el módulo de "Usuarios y roles":
# permite asignar roles sin tener que entrar a phpMyAdmin.
# ------------------------------------------------------------
@app.route("/api/actualizar_rol", methods=["POST"])
def actualizar_rol():
    datos = request.get_json()
    id_usuario = datos.get("id_usuario")       # quién hace la petición (debe ser admin)
    id_objetivo = datos.get("id_objetivo")      # a quién se le cambia el rol
    nuevo_rol = datos.get("nuevo_rol")

    ROLES_VALIDOS = ("cliente", "veterinario", "admin_tienda", "admin")

    if not id_usuario or not id_objetivo or nuevo_rol not in ROLES_VALIDOS:
        return jsonify({"exito": False, "mensaje": "Datos inválidos"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin",))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("UPDATE usuarios SET rol = %s WHERE id = %s", (nuevo_rol, id_objetivo))
        conexion.commit()

        return jsonify({"exito": True, "mensaje": "Rol actualizado correctamente"})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: TIENDA / PEDIDOS (panel de administrador)
# Formato de columnas: id_pedido, cliente, total, metodo_pago,
# estado, fecha
# ------------------------------------------------------------
@app.route("/api/pedidos", methods=["GET"])
def pedidos_admin():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin", "admin_tienda"))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("""
            SELECT p.id AS id_pedido,
                   u.nombre AS cliente,
                   p.total, p.metodo_pago, p.estado,
                   p.creado_en AS fecha
            FROM pedidos p
            INNER JOIN usuarios u ON p.id_usuario = u.id
            ORDER BY p.creado_en DESC
        """)
        pedidos = cursor.fetchall()

        for fila in pedidos:
            fila["fecha"] = fila["fecha"].strftime("%Y-%m-%d %H:%M")

        return jsonify({"exito": True, "resultados": pedidos})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: NOTIFICACIONES Y PROMOCIONES (panel de administrador)
# GET: lista el histórico enviado.
# Formato de columnas: id, titulo, tipo, enviada_a, fecha
# ------------------------------------------------------------
@app.route("/api/notificaciones_admin", methods=["GET"])
def notificaciones_admin():
    id_usuario = request.args.get("id_usuario")

    if not id_usuario:
        return jsonify({"exito": False, "mensaje": "Falta id_usuario"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin",))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        cursor.execute("""
            SELECT n.id, n.titulo, n.tipo,
                   u.nombre AS enviada_a,
                   n.creado_en AS fecha
            FROM notificaciones n
            INNER JOIN usuarios u ON n.id_usuario = u.id
            ORDER BY n.creado_en DESC
            LIMIT 100
        """)
        notificaciones = cursor.fetchall()

        for fila in notificaciones:
            fila["fecha"] = fila["fecha"].strftime("%Y-%m-%d %H:%M")

        return jsonify({"exito": True, "resultados": notificaciones})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()


# ------------------------------------------------------------
# API: ENVIAR NOTIFICACIÓN / PROMOCIÓN MASIVA (panel de admin)
# Envía la misma notificación a TODOS los usuarios activos,
# o solo a los que tengan rol 'cliente' si se marca soloClientes.
# ------------------------------------------------------------
@app.route("/api/enviar_promocion", methods=["POST"])
def enviar_promocion():
    datos = request.get_json()
    id_usuario = datos.get("id_usuario")
    titulo = datos.get("titulo")
    descripcion = datos.get("descripcion")
    solo_clientes = datos.get("solo_clientes", True)

    if not id_usuario or not titulo or not descripcion:
        return jsonify({"exito": False, "mensaje": "Faltan datos obligatorios"}), 400

    conexion = get_connection()
    cursor = conexion.cursor(dictionary=True)

    try:
        usuario = verificar_rol(cursor, id_usuario, ("admin",))
        if not usuario:
            return jsonify({"exito": False, "mensaje": "No autorizado"}), 403

        if solo_clientes:
            cursor.execute("SELECT id FROM usuarios WHERE activo = 1 AND rol = 'cliente'")
        else:
            cursor.execute("SELECT id FROM usuarios WHERE activo = 1")
        destinatarios = cursor.fetchall()

        for d in destinatarios:
            cursor.execute("""
                INSERT INTO notificaciones (id_usuario, titulo, descripcion, tipo, leida)
                VALUES (%s, %s, %s, 'promocion', 0)
            """, (d["id"], titulo, descripcion))
        conexion.commit()

        return jsonify({"exito": True, "mensaje": f"Notificación enviada a {len(destinatarios)} usuarios"})

    except mysql.connector.Error as err:
        return jsonify({"exito": False, "mensaje": str(err)}), 500
    finally:
        cursor.close()
        conexion.close()

# ------------------------------------------------------------
# INICIAR SERVIDOR (esto siempre va al final del archivo)
# ------------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True, port=5000)