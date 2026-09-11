#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
foco — puntero laser y foco de atencion en pantalla para Linux/X11,
        gobernado por un Logitech Spotlight 2.

Por que existe
--------------
Projecteur (github.com/gbin/Projecteur) resuelve esto muy bien, pero su linea
actual es exclusiva de Wayland con KDE Plasma 6.7+, y la rama Qt5/X11 es
anterior al Spotlight 2 y no conoce sus identificadores. Quien use XFCE, MATE,
Cinnamon o GNOME sobre X11 con un Spotlight 2 se queda sin nada.

Como funciona el mando (deducido midiendo, no documentado por Logitech)
----------------------------------------------------------------------
El Spotlight 2 por Bluetooth aparece como 046d:b506 y expone TRES canales:

  * teclado  -> KEY_LEFT / KEY_RIGHT, los botones de pasar diapositiva.
                Funcionan solos, sin driver.
  * raton    -> REL_X / REL_Y con los datos del giroscopio.
  * hidraw   -> tramas HID++ 2.0 en crudo.

La clave: **no existe una trama de "boton pulsado"**. Manteniendo el boton del
puntero, el giroscopio empieza a emitir movimiento; al soltarlo, para. Medido:
tres pulsaciones de unos cinco segundos dieron exactamente tres rafagas de
3,4 / 3,7 / 3,8 s, y treinta minutos de silencio entre medias.

O sea que **el propio flujo de movimiento es la senal**. Eso simplifica mucho
el programa: no hay que decodificar el giroscopio ni mover el cursor, porque el
kernel ya lo hace. Solo hay que dibujar donde ya esta el puntero.

Consumo
-------
En reposo el proceso duerme en el bucle de GLib sobre el descriptor del
dispositivo: cero despertares, cero CPU. La ventana translucida se crea al
activar el efecto y se destruye al soltar. Mientras esta activa se redibuja
como mucho a 60 Hz, coalesciendo los eventos de movimiento (que llegan a mas
del doble de ritmo).
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import math
import socket
import time
import os
import signal
import sys

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib  # noqa: E402

import evdev  # noqa: E402

# --------------------------------------------------------------------------
# Dispositivos conocidos. (proveedor, producto) -> descripcion
# Los identificadores del Spotlight 2 salen del README de Projecteur y estan
# confirmados sobre el aparato real.
# --------------------------------------------------------------------------
CONOCIDOS = {
    (0x046D, 0xB506): "Logitech Spotlight 2 (Bluetooth)",
    (0x046D, 0xC548): "Logitech Spotlight 2 (receptor Logi Bolt)",
    (0x046D, 0xC53E): "Logitech Spotlight (receptor unifying)",
    (0x046D, 0xB503): "Logitech Spotlight (Bluetooth)",
}

# Tiempo sin movimiento tras el cual se considera que se solto el boton.
# 250 ms: por encima del hueco entre eventos con el mando quieto (~10 ms) y por
# debajo de lo que se nota como retardo al soltar.
# HID++ 2.0: trama larga (0x11) del propio dispositivo (0xff) para el indice de
# funcion 0x0b, que en el Spotlight corresponde a "Reprogrammable Keys V4".
# Los parametros llevan el identificador de control pulsado; 0x0050 es el boton
# del puntero en su SEGUNDO nivel de presion. Todo ceros = soltado.
# Averiguado interrogando al aparato con getFeature/getCidInfo y confirmado
# midiendo pulsaciones reales:
#
#   0x1B04 "ReprogrammableKeysV4" esta en el indice 11 (0x0b)
#   CID 0x00D8 = boton del puntero, PRIMER nivel de presion (suave)
#   CID 0x01A8 = boton del puntero, SEGUNDO nivel (fuerte, el que vibra)
#
# Los dos estan declarados por el propio mando como "sensibles a la fuerza".
# Al soltar desde el nivel 2 se atraviesa el 1, asi que llega 0x01a8 -> 0x00d8
# -> nada, y hay que tratarlo como una transicion y no como dos pulsaciones.
HIDPP_INFORME = 0x11
IDX_1B04 = 0x0B
CID_NIVEL1 = 0x00D8
CID_NIVEL2 = 0x01A8
CIDS_DIVERTIBLES = (0x0050, 0x00D8, 0x01A8, 0x00D9, 0x00DA,
                    0x00DB, 0x00DC, 0x00FB, 0x00FC, 0x01B0)
SW_ID = 0x05

MS_INACTIVIDAD = 250
MS_FOTOGRAMA = 16          # ~60 Hz maximo mientras el efecto esta activo



class Cursor:
    """Esconde y recupera el puntero del sistema.

    Se usa la extension XFixes por ctypes en vez de anadir una dependencia:
    libX11 y libXfixes estan en cualquier maquina con X, y XFixesHideCursor es
    la unica forma limpia de esconder el cursor globalmente (poner un cursor
    vacio solo afecta a las ventanas propias, y la nuestra es transparente a
    los clics, asi que el puntero nunca "esta" sobre ella).

    Si algo falla, el programa sigue: quedarse sin efecto es molesto, pero
    quedarse sin cursor seria mucho peor, y por eso hay un atexit y ademas se
    restaura en cada apagado del efecto.
    """

    def __init__(self):
        self.ok = False
        self.oculto = False
        self.pendientes = 0      # peticiones de esconder sin deshacer
        try:
            self.x11 = ctypes.CDLL("libX11.so.6")
            self.xf = ctypes.CDLL("libXfixes.so.3")
            self.x11.XOpenDisplay.restype = ctypes.c_void_p
            self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
            self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            self.dpy = self.x11.XOpenDisplay(None)
            if not self.dpy:
                return
            self.raiz = self.x11.XDefaultRootWindow(ctypes.c_void_p(self.dpy))
            ev, err = ctypes.c_int(), ctypes.c_int()
            self.xf.XFixesQueryExtension.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int)]
            if not self.xf.XFixesQueryExtension(ctypes.c_void_p(self.dpy),
                                                ctypes.byref(ev), ctypes.byref(err)):
                return
            for f in (self.xf.XFixesHideCursor, self.xf.XFixesShowCursor):
                f.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            self.ok = True
            atexit.register(self.mostrar)
        except OSError:
            self.ok = False

    def _llamar(self, f):
        f(ctypes.c_void_p(self.dpy), self.raiz)
        self.x11.XFlush(ctypes.c_void_p(self.dpy))

    def ocultar(self):
        """Esconde el puntero, llevando la cuenta.

        XFixes CUENTA las peticiones por cliente: el cursor sigue escondido
        mientras queden peticiones sin deshacer. Si se pide esconder N veces y
        mostrar una sola, el usuario se queda sin raton. Por eso aqui se
        registra cuantas van y se deshacen todas de golpe.
        """
        if not self.ok or self.oculto:
            return
        self._llamar(self.xf.XFixesHideCursor)
        self.pendientes += 1
        self.oculto = True

    def mostrar(self):
        """Deshace TODAS las peticiones pendientes.

        Se manda alguna de mas a proposito (las sobrantes no hacen nada):
        pasarse es inofensivo, quedarse corto deja al usuario sin puntero.
        """
        if not self.ok:
            return
        for _ in range(self.pendientes + 4):
            self._llamar(self.xf.XFixesShowCursor)
        self.pendientes = 0
        self.oculto = False


class Efecto:
    """Los modos de dibujo. Cada uno pinta sobre un lienzo cairo."""

    SPOTLIGHT = "spotlight"
    LASER = "laser"
    AMBOS = "ambos"
    TODOS = (SPOTLIGHT, LASER, AMBOS)


class Superposicion(Gtk.Window):
    """Ventana a pantalla completa, translucida y transparente al raton."""

    def __init__(self, modo: str, radio: int, oscurecer: float, traza=False,
                 radio_laser: int = 13):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.traza = traza
        self.radio_laser = radio_laser
        self.modo = modo
        self.radio = radio
        self.oscurecer = oscurecer
        self.x = self.y = 0

        pantalla = Gdk.Screen.get_default()
        visual = pantalla.get_rgba_visual()
        if visual is None or not pantalla.is_composited():
            raise RuntimeError(
                "hace falta un compositor activo.\n"
                "En XFCE: Configuracion > Afinador de ventanas > Compositor."
            )
        self.set_visual(visual)
        self.set_app_paintable(True)
        self.set_keep_above(True)
        self.set_accept_focus(False)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)

        self.an = pantalla.get_width()
        self.al = pantalla.get_height()
        self.move(0, 0)
        self.set_default_size(self.an, self.al)

        self.connect("draw", self._dibujar)
        self.connect("realize", self._al_realizar)

    def _al_realizar(self, _w):
        # Region de entrada vacia: los clics atraviesan la ventana como si no
        # estuviera. Sin esto, el efecto bloquearia el raton por completo.
        region = Gdk.cairo_region_create_from_surface(
            __import__("cairo").ImageSurface(__import__("cairo").FORMAT_A1, 0, 0))
        self.input_shape_combine_region(region)

    def situar(self, x: int, y: int):
        self.x, self.y = x, y

    def _dibujar(self, _w, cr):
        import cairo
        if self.traza:
            print(f"dibujo modo={self.modo} en ({self.x},{self.y}) "
                  f"r={self.radio} velo={self.oscurecer}", file=sys.stderr, flush=True)
        cr.set_operator(cairo.OPERATOR_SOURCE)

        if self.modo in (Efecto.SPOTLIGHT, Efecto.AMBOS):
            # Velo oscuro sobre todo...
            cr.set_source_rgba(0, 0, 0, self.oscurecer)
            cr.paint()
            # ...y un agujero limpio alrededor del puntero. El borde se difumina
            # con un degradado radial para que no se vea un recorte duro.
            deg = cairo.RadialGradient(self.x, self.y, self.radio * 0.72,
                                       self.x, self.y, self.radio)
            deg.add_color_stop_rgba(0, 0, 0, 0, 0)
            deg.add_color_stop_rgba(1, 0, 0, 0, self.oscurecer)
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.arc(self.x, self.y, self.radio, 0, 2 * math.pi)
            cr.set_source(deg)
            cr.fill()
        else:
            cr.set_source_rgba(0, 0, 0, 0)
            cr.paint()

        if self.modo in (Efecto.LASER, Efecto.AMBOS):
            cr.set_operator(cairo.OPERATOR_OVER)
            r = self.radio_laser
            # Halo amplio y suave: es lo que lo hace visible en un proyector,
            # donde el punto solo se pierde.
            halo = cairo.RadialGradient(self.x, self.y, 0, self.x, self.y, r * 3.2)
            halo.add_color_stop_rgba(0.0, 1.0, 0.15, 0.15, 0.50)
            halo.add_color_stop_rgba(0.5, 1.0, 0.10, 0.10, 0.22)
            halo.add_color_stop_rgba(1.0, 1.0, 0.10, 0.10, 0.0)
            cr.set_source(halo)
            cr.arc(self.x, self.y, r * 3.2, 0, 2 * math.pi)
            cr.fill()
            # Cuerpo del punto
            cr.set_source_rgba(1.0, 0.20, 0.20, 0.95)
            cr.arc(self.x, self.y, r, 0, 2 * math.pi)
            cr.fill()
            # Nucleo claro, que da la sensacion de brillo
            cr.set_source_rgba(1.0, 0.80, 0.78, 0.98)
            cr.arc(self.x, self.y, r * 0.38, 0, 2 * math.pi)
            cr.fill()
        return False


class Foco:
    def __init__(self, args):
        self.modo = args.modo
        self.modo_suave = args.modo_suave
        self.modo_fuerte = args.modo_fuerte
        self.radio_laser = args.radio_laser
        self.ocultar_cursor = args.ocultar_cursor
        self.cursor = Cursor()
        self.radio = args.radio
        self.oscurecer = args.oscurecer
        self.verboso = args.verboso

        self.ventana: Superposicion | None = None
        self.dispositivo: evdev.InputDevice | None = None
        self.vigilante = None
        self.temporizador_off = None
        self.temporizador_pintar = None
        self.sucio = False
        # "Fijado" = encendido a mano con un atajo de teclado. A diferencia del
        # mando, aqui no hay boton que soltar, asi que el efecto se queda hasta
        # que se apague explicitamente.
        self.fijado = False

        self.hid_fd = None
        self.hid_vigilante = None
        self.boton_fuerte = False
        self.boton_nivel1 = False
        self.idx_haptic = 17          # 0x1A01 PresenterHaptic

        self._buscar_dispositivo()
        self._abrir_hidraw()
        self._vigilar_conexiones()
        self._abrir_socket()

    # ------------------------------------------------------------- registro
    def log(self, *a):
        if self.verboso:
            print(*a, file=sys.stderr, flush=True)

    # ---------------------------------------------------------- dispositivo
    def _buscar_dispositivo(self):
        for ruta in evdev.list_devices():
            try:
                d = evdev.InputDevice(ruta)
            except OSError:
                continue
            clave = (d.info.vendor, d.info.product)
            if clave not in CONOCIDOS:
                continue
            # Queremos el nodo que emite movimiento, no el de teclado.
            caps = d.capabilities()
            if evdev.ecodes.EV_REL not in caps:
                continue
            if evdev.ecodes.REL_X not in caps[evdev.ecodes.EV_REL]:
                continue
            self.dispositivo = d
            self.log(f"mando: {CONOCIDOS[clave]} en {ruta} ({d.name})")
            self.vigilante = GLib.io_add_watch(
                d.fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP,
                self._hay_eventos)
            return True
        self.log("mando no encontrado; esperando a que aparezca")
        return False

    def _vigilar_conexiones(self):
        """Reaccionar a que el mando aparezca o desaparezca.

        El Spotlight 2 se duerme por Bluetooth cuando no se usa y su nodo de
        /dev/input desaparece. Se usa udev, que avisa por descriptor, en vez de
        reintentar en bucle: asi el proceso sigue sin despertarse para nada.
        """
        try:
            import pyudev
        except ImportError:
            self.log("pyudev no disponible: no habra reconexion automatica")
            return
        ctx = pyudev.Context()
        mon = pyudev.Monitor.from_netlink(ctx)
        mon.filter_by(subsystem="input")
        mon.start()
        self._udev = mon
        GLib.io_add_watch(mon.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                          self._cambio_udev)

    def _cambio_udev(self, _fd, _cond):
        dev = self._udev.poll(timeout=0)
        while dev is not None:
            if dev.action == "add" and self.dispositivo is None:
                GLib.timeout_add(400, self._reintentar)
            dev = self._udev.poll(timeout=0)
        return True

    def _reintentar(self):
        if self.dispositivo is None:
            if self._buscar_dispositivo():
                self._abrir_hidraw()
        return False

    def _soltar_dispositivo(self):
        if self.vigilante:
            GLib.source_remove(self.vigilante)
            self.vigilante = None
        if self.dispositivo:
            try:
                self.dispositivo.close()
            except Exception:
                pass
        self.dispositivo = None
        self._cerrar_hidraw()
        self._desactivar()
        self.log("mando desconectado")

    # -------------------------------------------------------------- eventos
    def _hay_eventos(self, _fd, cond):
        if cond & (GLib.IO_ERR | GLib.IO_HUP):
            self._soltar_dispositivo()
            return False
        try:
            hubo_movimiento = False
            for e in self.dispositivo.read():
                if e.type == evdev.ecodes.EV_REL:
                    if e.code in (evdev.ecodes.REL_X, evdev.ecodes.REL_Y):
                        hubo_movimiento = True
                    elif e.code == evdev.ecodes.REL_WHEEL and self.ventana:
                        # Si el mando tuviera rueda, ajusta el radio del foco.
                        self.radio = max(40, min(600, self.radio + e.value * 15))
                        self.sucio = True
        except OSError:
            self._soltar_dispositivo()
            return False

        if hubo_movimiento:
            self._activar()
        return True

    # --------------------------------------------------------------- efecto
    def _activar(self, fijado: bool = False):
        if fijado:
            self.fijado = True
        if self.ventana is None:
            try:
                self.ventana = Superposicion(self.modo, self.radio, self.oscurecer,
                                             traza=self.verboso,
                                             radio_laser=self.radio_laser)
            except RuntimeError as e:
                print(f"foco: {e}", file=sys.stderr)
                return
            self._situar_en_puntero()      # antes de mostrar, no despues
            self.ventana.show_all()
            self._ajustar_cursor()
            self.log("efecto activado")
            # Pintar solo mientras el efecto esta vivo.
            self.temporizador_pintar = GLib.timeout_add(MS_FOTOGRAMA, self._pintar)

        elif not self.ventana.get_visible():
            self._situar_en_puntero()
            self.ventana.show_all()
            self._ajustar_cursor()
            if self.temporizador_pintar is None:
                self.temporizador_pintar = GLib.timeout_add(MS_FOTOGRAMA,
                                                            self._pintar)
            self.log("efecto activado")

        self.sucio = True
        # Reiniciar la cuenta atras de "se ha soltado el boton". Si el efecto
        # esta fijado por teclado no hay cuenta atras que valga.
        if self.temporizador_off:
            GLib.source_remove(self.temporizador_off)
            self.temporizador_off = None
        if not self.fijado:
            self.temporizador_off = GLib.timeout_add(MS_INACTIVIDAD,
                                                     self._desactivar)

    def _ajustar_cursor(self):
        """El laser sustituye al puntero; no deben verse los dos."""
        visible = self.ventana is not None and self.ventana.get_visible()
        quiere = (visible and (
            self.ocultar_cursor == "siempre"
            or (self.ocultar_cursor == "laser"
                and self.modo in (Efecto.LASER, Efecto.AMBOS))))
        if quiere:
            self.cursor.ocultar()
        else:
            self.cursor.mostrar()

    def _situar_en_puntero(self):
        try:
            disp = Gdk.Display.get_default()
            _p, x, y = disp.get_default_seat().get_pointer().get_position()
            self.ventana.situar(x, y)
            self.ventana.radio = self.radio
        except Exception as e:          # noqa: BLE001
            self.log(f"no se pudo leer el puntero: {e!r}")

    def _pintar(self):
        """Coalesce el movimiento: se redibuja como mucho una vez por fotograma.

        El mando manda mas de 120 eventos por segundo; redibujar la pantalla
        entera en cada uno seria tirar CPU y bateria sin que se note en el
        resultado.
        """
        if self.ventana is None or not self.ventana.get_visible():
            self.temporizador_pintar = None
            return False

        try:
            # Se consulta el puntero en cada fotograma en vez de fiarse solo de
            # los eventos del mando: activado por teclado, el cursor lo mueve el
            # touchpad y el efecto tiene que seguirlo igual. Es una consulta al
            # servidor X por fotograma, y solo mientras el efecto esta visible.
            disp = Gdk.Display.get_default()
            _pantalla, x, y = disp.get_default_seat().get_pointer().get_position()
            if (x, y) != (self.ventana.x, self.ventana.y) or self.sucio:
                self.ventana.situar(x, y)
                self.ventana.radio = self.radio
                self.ventana.queue_draw()
                self.sucio = False
        except Exception as e:          # noqa: BLE001
            self.log(f"fallo al repintar: {e!r}")
        return True

    def _desactivar(self, forzar: bool = False):
        if self.fijado and not forzar:
            return False
        if self.boton_nivel1 and not forzar:
            # El dedo sigue en el boton. Quedarse quieto no es soltar.
            self.temporizador_off = None
            return False
        self.fijado = False
        self.temporizador_off = None
        if self.temporizador_pintar is not None:
            # Puede haberse retirado solo si el callback devolvio False.
            try:
                GLib.source_remove(self.temporizador_pintar)
            except (ValueError, GLib.GError):
                pass
            self.temporizador_pintar = None
        if self.ventana is not None:
            # Ocultar, no destruir. Crear y destruir una ventana a pantalla
            # completa muchas veces por minuto hace que el compositor deje
            # restos en pantalla. Oculta no se compone ni se dibuja, asi que
            # el ahorro es el mismo.
            if self.ventana.get_visible():
                self.ventana.hide()
                self.cursor.mostrar()
                self.log("efecto desactivado")
        return False

    # ----------------------------------------------------- segundo nivel
    def _abrir_hidraw(self):
        """Canal HID++ crudo: es el unico sitio donde se ve la pulsacion fuerte.

        El boton del Spotlight tiene dos niveles. El primero enciende el
        giroscopio y ya se detecta por el flujo de movimiento. El segundo NO
        genera ningun evento de entrada: solo una trama HID++. En Windows es lo
        que hace vibrar el mando y cambiar de efecto.

        Si no se puede leer (falta la regla de udev), el programa sigue
        funcionando; simplemente no habra cambio de efecto desde el mando.
        """
        import glob
        for ruta in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            try:
                uevent = open(os.path.join(ruta, "device", "uevent")).read()
            except OSError:
                continue
            # HID_ID viene como "0005:0000046D:0000B506": bus, proveedor y
            # producto con ocho digitos, no cuatro. Se parsea en vez de buscar
            # una subcadena, que es como fallaba antes.
            ident = None
            for linea in uevent.splitlines():
                if linea.startswith("HID_ID="):
                    partes = linea.split("=", 1)[1].split(":")
                    if len(partes) == 3:
                        ident = (int(partes[1], 16), int(partes[2], 16))
                    break
            if ident not in CONOCIDOS:
                continue
            nodo = "/dev/" + os.path.basename(ruta)
            try:
                # Lectura Y escritura: hay que poder mandarle el setCidReporting,
                # no solo escuchar. Con O_RDONLY el write da EBADF.
                self.hid_fd = os.open(nodo, os.O_RDWR | os.O_NONBLOCK)
            except OSError as e:
                self.log(f"no se puede leer {nodo} ({e.strerror}); "
                         f"sin cambio de efecto desde el mando. "
                         f"Instala 99-spotlight.rules")
                return
            self.log(f"canal HID++ en {nodo}")
            self._desviar_botones()
            self.hid_vigilante = GLib.io_add_watch(
                self.hid_fd, GLib.PRIORITY_DEFAULT,
                GLib.IO_IN | GLib.IO_ERR | GLib.IO_HUP, self._hay_hidpp)
            return

    def _hidpp_largo(self, idx_car, funcion, *params):
        """Arma un informe HID++ 2.0 largo.

        El Spotlight 2 NO declara el informe corto (0x10) en su descriptor:
        solo 0x01, 0x02, 0x03 y 0x11. Mandarle informes cortos no da error, da
        silencio, que es peor de depurar.
        """
        t = bytearray(20)
        t[0], t[1], t[2], t[3] = HIDPP_INFORME, 0xFF, idx_car, (funcion << 4) | SW_ID
        for i, v in enumerate(params[:16]):
            t[4 + i] = v
        return bytes(t)

    def _desviar_botones(self):
        """Pide al mando que notifique sus botones por HID++.

        Sin esto el aparato esta mudo: los controles reprogramables de Logitech
        no avisan de nada hasta que el ordenador lo pide con setCidReporting.
        Es exactamente lo que hace Logi Options+ en Windows al arrancar, y la
        razon de que en Linux "el boton no haga nada".
        """
        if self.hid_fd is None:
            return
        # Solo los dos que se usan. Mandar los diez de golpe hacia que el
        # aparato perdiera comandos: no lee tan rapido y no da error, los
        # descarta en silencio. Con dos y una pausa entre ellos va sobrado.
        ok = 0
        for cid in (CID_NIVEL1, CID_NIVEL2):
            trama = self._hidpp_largo(IDX_1B04, 3, (cid >> 8) & 0xFF, cid & 0xFF, 0x23)
            try:
                os.write(self.hid_fd, trama)
                time.sleep(0.04)      # una sola vez al conectar, no en caliente
                ok += 1
            except OSError as e:
                self.log(f"no se pudo desviar 0x{cid:04x}: {e}")
        self.log(f"botones desviados a HID++: {ok}/2")

    def _hay_hidpp(self, _fd, cond):
        if cond & (GLib.IO_ERR | GLib.IO_HUP):
            self._cerrar_hidraw()
            return False
        try:
            datos = os.read(self.hid_fd, 64)
        except (BlockingIOError, OSError):
            return True
        if len(datos) < 6 or datos[0] != HIDPP_INFORME or datos[2] != IDX_1B04:
            return True

        funcion = datos[3] >> 4
        if funcion != 0:
            # funcion 1 = divertedRawXY: el giroscopio en crudo. No hace falta,
            # el kernel ya mueve el cursor con el canal de raton.
            return True

        # divertedButtonsEvent: hasta cuatro CIDs pulsados a la vez, 0 = nada.
        cids = {int.from_bytes(datos[4 + i * 2:6 + i * 2], "big") for i in range(4)}
        cids.discard(0)

        nivel2 = CID_NIVEL2 in cids
        nivel1 = CID_NIVEL1 in cids or nivel2      # el 2 implica el 1

        # Cada nivel de presion muestra SIEMPRE el mismo efecto.
        #
        # Antes la presion fuerte rotaba al siguiente efecto. Era un mal
        # disenyo: el modo es un estado invisible que cambiaba bajo los pies
        # del usuario, asi que la misma pulsacion suave daba un resultado
        # distinto cada vez y parecia averiado. Ahora el mando es predecible y
        # rotar entre efectos se deja al teclado, donde se hace a proposito.
        if nivel2 and not self.boton_fuerte:
            self.cambiar_modo(self.modo_fuerte)
            self._vibrar()
        elif nivel1 and not self.boton_nivel1:
            self.cambiar_modo(self.modo_suave)
        self.boton_fuerte = nivel2

        if nivel1:
            self._activar()
        elif self.boton_nivel1 and not self.fijado:
            # Soltado del todo: apagar sin esperar al temporizador, que asi va
            # mas fino que deducirlo de que dejen de llegar movimientos.
            self._desactivar(forzar=True)
        self.boton_nivel1 = nivel1
        return True

    def _vibrar(self):
        """Aviso haptico, como en Windows.

        La caracteristica 0x1A01 "PresenterHaptic" existe en este mando
        (indice 17). Si el comando no le gusta, se ignora en silencio: vibrar
        es un adorno y no debe tumbar nada.
        """
        if self.hid_fd is None or self.idx_haptic is None:
            return
        try:
            os.write(self.hid_fd,
                     self._hidpp_largo(self.idx_haptic, 1, 0x01, 0x00, 0x00))
        except OSError:
            pass

    def _cerrar_hidraw(self):
        if self.hid_vigilante:
            GLib.source_remove(self.hid_vigilante)
            self.hid_vigilante = None
        if self.hid_fd is not None:
            try:
                os.close(self.hid_fd)
            except OSError:
                pass
        self.hid_fd = None

    def _siguiente_efecto(self):
        orden = list(Efecto.TODOS)
        self.cambiar_modo(orden[(orden.index(self.modo) + 1) % len(orden)])

    # ------------------------------------------------- ordenes por socket
    def _abrir_socket(self):
        """Canal para que un atajo de teclado hable con el proceso.

        Se usa un socket de dominio Unix en XDG_RUNTIME_DIR: no necesita
        permisos especiales, desaparece al reiniciar y no obliga a tener un
        servicio de escucha en red para algo puramente local.
        """
        base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/foco-{os.getuid()}"
        os.makedirs(base, exist_ok=True)
        self.ruta_socket = os.path.join(base, "foco.sock")
        try:
            os.unlink(self.ruta_socket)
        except FileNotFoundError:
            pass
        self.servidor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.servidor.bind(self.ruta_socket)
        self.servidor.setblocking(False)
        GLib.io_add_watch(self.servidor.fileno(), GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN, self._hay_orden)
        self.log(f"escuchando ordenes en {self.ruta_socket}")

    def _visible(self) -> bool:
        """Si el efecto se esta viendo ahora mismo.

        Desde que la ventana se reutiliza en vez de destruirse, que exista ya
        no quiere decir que se vea: puede estar simplemente oculta.
        """
        return self.ventana is not None and self.ventana.get_visible()

    def _hay_orden(self, _fd, _cond):
        try:
            datos, _ = self.servidor.recvfrom(256)
        except BlockingIOError:
            return True
        orden = datos.decode("utf8", "replace").strip()
        self.log(f"orden: {orden}")

        if orden == "alternar":
            if self._visible() and self.fijado:
                self._desactivar(forzar=True)
            else:
                self._activar(fijado=True)
        elif orden == "siguiente":
            self._siguiente_efecto()
        elif orden in Efecto.TODOS:
            self.cambiar_modo(orden)
            if not self._visible():
                self._activar(fijado=True)
        elif orden == "mas":
            self.radio = min(600, self.radio + 30); self.sucio = True
        elif orden == "menos":
            self.radio = max(40, self.radio - 30); self.sucio = True
        elif orden == "cursor":
            self.cursor.mostrar()
        elif orden == "apagar":
            self._desactivar(forzar=True)
        elif orden == "salir":
            Gtk.main_quit()
        return True

    # ------------------------------------------------------------ mandos IPC
    def cambiar_modo(self, modo: str):
        if modo == self.modo:
            return
        self.modo = modo
        if self.ventana:
            self.ventana.modo = modo
            self.sucio = True
        self._ajustar_cursor()
        self.log(f"modo: {modo}")


def main():
    p = argparse.ArgumentParser(
        prog="foco",
        description="Puntero laser y foco de atencion para X11, "
                    "gobernado por un Logitech Spotlight 2.")
    p.add_argument("-m", "--modo", choices=Efecto.TODOS, default=Efecto.SPOTLIGHT,
                   help="efecto a mostrar (por defecto: spotlight)")
    p.add_argument("--modo-suave", choices=Efecto.TODOS, default=Efecto.SPOTLIGHT,
                   dest="modo_suave",
                   help="efecto de la presion suave del mando (por defecto: spotlight)")
    p.add_argument("--modo-fuerte", choices=Efecto.TODOS, default=Efecto.LASER,
                   dest="modo_fuerte",
                   help="efecto de la presion fuerte del mando (por defecto: laser)")
    p.add_argument("--radio-laser", type=int, default=13, dest="radio_laser",
                   help="radio del punto del laser en pixeles (por defecto: 13)")
    p.add_argument("--ocultar-cursor", choices=("laser", "siempre", "nunca"),
                   default="laser", dest="ocultar_cursor",
                   help="cuando esconder el puntero del sistema "
                        "(por defecto: solo con el laser)")
    p.add_argument("-r", "--radio", type=int, default=180,
                   help="radio del foco en pixeles (por defecto: 180)")
    p.add_argument("-o", "--oscurecer", type=float, default=0.72,
                   help="opacidad del velo, de 0 a 1 (por defecto: 0.72)")
    p.add_argument("-v", "--verboso", action="store_true",
                   help="explicar por la salida de error lo que va pasando")
    p.add_argument("--enviar", metavar="ORDEN",
                   help="enviar una orden al proceso ya en marcha: alternar, "
                        "siguiente, spotlight, laser, ambos, mas, menos, "
                        "apagar, cursor, salir")
    p.add_argument("--recuperar-cursor", action="store_true",
                   dest="recuperar_cursor",
                   help="rescate: devuelve el puntero del raton si se quedo "
                        "escondido. Funciona aunque foco no este corriendo.")
    p.add_argument("--listar", action="store_true",
                   help="listar los dispositivos de entrada y salir")
    args = p.parse_args()

    if args.recuperar_cursor:
        # Independiente del resto: si el proceso murio con el cursor escondido,
        # el usuario necesita poder recuperarlo sin depender de ese proceso.
        c = Cursor()
        if not c.ok:
            print("foco: no se pudo hablar con XFixes", file=sys.stderr)
            return 1
        for _ in range(500):
            c._llamar(c.xf.XFixesShowCursor)
        print("cursor restaurado")
        return 0

    if args.enviar:
        base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/foco-{os.getuid()}"
        ruta = os.path.join(base, "foco.sock")
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            c.sendto(args.enviar.encode(), ruta)
        except (FileNotFoundError, ConnectionRefusedError):
            print("foco: no hay ningun proceso escuchando.\n"
                  "      Arranca 'foco' primero (o revisa el autoarranque).",
                  file=sys.stderr)
            return 1
        return 0

    if args.listar:
        for ruta in evdev.list_devices():
            try:
                d = evdev.InputDevice(ruta)
            except OSError:
                continue
            clave = (d.info.vendor, d.info.product)
            marca = "  <-- compatible" if clave in CONOCIDOS else ""
            print(f"{ruta:20} {d.name:<34} "
                  f"{d.info.vendor:04x}:{d.info.product:04x}{marca}")
        return 0

    signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())
    signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())

    foco = Foco(args)
    # Red de seguridad: quedarse sin cursor seria mucho peor que quedarse sin
    # efecto, asi que se restaura tambien al salir por señal.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (foco.cursor.mostrar(), Gtk.main_quit()))
    if foco.dispositivo is None:
        print("foco: no encuentro ningun mando compatible.\n"
              "      Conectalo y vuelve a intentarlo, o mira 'foco --listar'.\n"
              "      Si sale pero no se puede leer, falta estar en el grupo "
              "'input':  sudo usermod -aG input $USER", file=sys.stderr)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
