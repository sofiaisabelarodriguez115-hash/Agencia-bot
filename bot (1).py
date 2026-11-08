"""
Agencia de Noticias HN — actualizador diario de la página.

Junta los titulares más recientes de varios medios (por RSS), los ordena en
"Honduras" e "Internacional" y actualiza index.html (la página con los
personajes). Si además configuras Telegram, también los manda a un canal.

Solo usa la biblioteca estándar de Python (no hay que instalar nada).

Variables de entorno:
  TELEGRAM_TOKEN    (opcional) token del bot que te da @BotFather
  TELEGRAM_CHAT_ID  (opcional) tu canal de Telegram
  DRY_RUN=1         (opcional) imprime el mensaje en vez de enviarlo
"""

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ---------------------------------------------------------------------------
# CONFIGURACIÓN — puedes editar todo esto
# ---------------------------------------------------------------------------

HORAS_ATRAS = 36          # solo noticias de las últimas X horas
POR_MEDIO = 3             # máximo de titulares por medio
MAX_POR_SECCION = 10      # máximo de titulares por sección

HN_TZ = timezone(timedelta(hours=-6))  # Honduras (no usa horario de verano)


def google_news(sitio):
    """RSS de Google Noticias filtrado por un sitio (sirve para medios sin RSS propio)."""
    q = urllib.parse.quote(f"site:{sitio} when:1d")
    return f"https://news.google.com/rss/search?q={q}&hl=es-419&gl=HN&ceid=HN:es-419"


# Cada medio tiene una lista de enlaces RSS: se prueba el primero y, si falla,
# el siguiente. Así el bot sigue funcionando aunque un medio cambie su RSS.
MEDIOS = {
    "honduras": [
        ("La Prensa",       [google_news("laprensa.hn")]),
        ("El Heraldo",      [google_news("elheraldo.hn")]),
        ("Proceso Digital", ["https://proceso.hn/feed/", google_news("proceso.hn")]),
        ("La Tribuna",      [google_news("latribuna.hn")]),
        ("Criterio.hn",     ["https://criterio.hn/feed/", google_news("criterio.hn")]),
        ("Tu Nota",         [google_news("tunota.com")]),
    ],
    "internacional": [
        ("BBC Mundo",       ["https://feeds.bbci.co.uk/mundo/rss.xml"]),
        ("DW Español",      ["https://rss.dw.com/rdf/rss-sp-all", google_news("dw.com/es")]),
        ("France 24",       ["https://www.france24.com/es/rss"]),
        ("EFE",             [google_news("efe.com")]),
    ],
}

TITULOS = {"honduras": "🇭🇳 HONDURAS", "internacional": "🌎 INTERNACIONAL"}

# ---------------------------------------------------------------------------
# LECTURA DE RSS
# ---------------------------------------------------------------------------

USER_AGENT = "Mozilla/5.0 (compatible; AgenciaNoticiasBot/1.0)"
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def descargar(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def _hijo(elem, nombre):
    """Busca un hijo por nombre sin importar el namespace (RSS, RDF o Atom)."""
    for h in elem:
        if h.tag.split("}")[-1] == nombre:
            return h
    return None


def _fecha(texto):
    if not texto:
        return None
    texto = texto.strip()
    try:
        d = parsedate_to_datetime(texto)          # formato RSS
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(texto.replace("Z", "+00:00"))  # Atom / RDF
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def leer_rss(contenido, medio):
    raiz = ET.fromstring(contenido)
    noticias = []
    for item in raiz.iter():
        if item.tag.split("}")[-1] not in ("item", "entry"):
            continue
        t = _hijo(item, "title")
        titulo = (t.text or "").strip() if t is not None else ""
        l = _hijo(item, "link")
        link = ""
        if l is not None:
            link = (l.text or "").strip() or l.get("href", "")
        f = None
        for campo in ("pubDate", "date", "published", "updated"):
            c = _hijo(item, campo)
            if c is not None and c.text:
                f = _fecha(c.text)
                break
        if not titulo or not link.startswith(("https://", "http://")):
            continue  # descarta enlaces raros (seguridad)
        d = _hijo(item, "description")
        if d is None:
            d = _hijo(item, "summary")
        resumen = ""
        if d is not None and d.text:
            resumen = html.unescape(re.sub(r"<[^>]+>", " ", d.text))
            resumen = " ".join(resumen.split())
            if len(resumen) > 220:
                resumen = resumen[:217].rsplit(" ", 1)[0] + "…"
        # Google Noticias agrega " - Nombre del medio" al final del título
        titulo = re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", titulo)
        titulo = html.unescape(titulo)
        # Google Noticias repite el título como descripción: en ese caso no sirve
        if _clave(resumen).startswith(_clave(titulo)[:30]):
            resumen = ""
        noticias.append({"titulo": titulo, "link": link, "fecha": f,
                         "medio": medio, "resumen": resumen})
    return noticias


def titulares_de(medio, urls):
    for url in urls:
        try:
            return leer_rss(descargar(url), medio)
        except Exception as e:  # noqa: BLE001 — si un medio falla, seguimos
            print(f"[aviso] {medio}: no se pudo leer {url} ({e})", file=sys.stderr)
    return []


def _clave(titulo):
    t = re.sub(r"[^a-z0-9áéíóúñü ]", "", titulo.lower())
    return " ".join(t.split()[:8])


def juntar(seccion, ahora, vistos):
    limite = ahora - timedelta(hours=HORAS_ATRAS)
    salida = []
    por_medio = []
    for medio, urls in MEDIOS[seccion]:
        lista = titulares_de(medio, urls)
        lista = [n for n in lista if n["fecha"] is None or n["fecha"] >= limite]
        lista.sort(key=lambda n: n["fecha"] or limite, reverse=True)
        por_medio.append(lista)

    # Alterna medios (1º de cada uno, luego 2º de cada uno...) para que haya variedad
    for ronda in range(POR_MEDIO):
        for lista in por_medio:
            if ronda < len(lista):
                n = lista[ronda]
                k = _clave(n["titulo"])
                if k in vistos:
                    continue
                vistos.add(k)
                salida.append(n)
    return salida[:MAX_POR_SECCION]


# ---------------------------------------------------------------------------
# PÁGINA WEB (index.html con los muñequitos)
# ---------------------------------------------------------------------------

CATEGORIAS = [
    ("Economía",   r"d[oó]lar|lempira|\bbch\b|econom|precio|combustible|gasolina|di[eé]sel|remesa|\bfmi\b|inflaci|bolsa|mercado|aranc|export|import|empleo|salario|banco"),
    ("Seguridad",  r"polic|homicid|crimen|asesin|muert|captur|extorsi|pandill|maras?\b|violencia|balacera|narco|detenid|c[aá]rcel|penitenciar"),
    ("Clima",      r"lluvia|copeco|cenaos|clima|tormenta|hurac[aá]n|frente fr[ií]o|temperatura|inundaci|sismo|temblor|sequ[ií]a"),
    ("Educación",  r"universidad|\bunah\b|\bumh\b|escuela|colegio|estudiant|maestr|docente|educaci|clases|beca"),
    ("Migración",  r"migra|deport|frontera|retornad|asilo|\bice\b|visa|tps"),
    ("Deportes",   r"f[uú]tbol|olimpia|motagua|marath[oó]n|real espa[ñn]a|selecci[oó]n|liga|mundial|\bgol\b|torneo|jugador|entrenador"),
    ("Tecnología", r"tecnolog|inteligencia artificial|\bia\b|ciencia|cient[ií]fic|espacial|nasa|internet|celular|app\b|ciber|robot"),
    ("Cultura",    r"cultur|m[uú]sica|cine|pel[ií]cula|libro|premio|festival|arte|concierto|museo"),
    ("Política",   r"congreso|presiden|gobierno|elecci|partido|diputad|ministr|alcald|senado|pol[ií]tic|canciller|cumbre|ley\b"),
]


def categoria(titulo):
    t = titulo.lower()
    for cat, patron in CATEGORIAS:
        if re.search(patron, t):
            return cat
    return "Política"


def dolar():
    """Tipo de cambio de referencia (open.er-api.com, gratis y sin clave)."""
    try:
        datos = json.loads(descargar("https://open.er-api.com/v6/latest/USD"))
        hnl = datos["rates"]["HNL"]
        return {"titulo": "DATO ÚTIL DEL DÍA", "valor": f"$1 = L{hnl:.2f}",
                "nota": "Tipo de cambio de referencia internacional (el oficial lo publica el BCH)",
                "url": "https://www.bch.hn/"}
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] dólar: {e}", file=sys.stderr)
        return {"titulo": "DATO ÚTIL DEL DÍA", "valor": "Ver BCH",
                "nota": "Consulta el precio oficial del dólar en el Banco Central",
                "url": "https://www.bch.hn/"}


def armar_quiz(noticias):
    import random
    medios = [m for s in MEDIOS.values() for m, _ in s]
    quiz, usados = [], set()
    random.shuffle(noticias := list(noticias))
    for n in noticias:
        if n["fuente"] in usados:
            continue
        usados.add(n["fuente"])
        otros = random.sample([m for m in medios if m != n["fuente"]], 2)
        ops = otros + [n["fuente"]]
        random.shuffle(ops)
        quiz.append({"q": f"¿Qué medio publicó esta noticia? «{n['titulo']}»",
                     "ops": ops, "ok": ops.index(n["fuente"]),
                     "exp": f"La publicó {n['fuente']}."})
        if len(quiz) == 3:
            break
    return quiz


def datos_pagina(secciones, ahora):
    hoy = ahora.astimezone(HN_TZ)
    noticias = []
    for seccion, lista in secciones.items():
        for n in lista:
            cat = categoria(n["titulo"])
            por = "Erik" if cat == "Economía" else ("José Joel" if seccion == "honduras" else "Óscar")
            noticias.append({"seccion": seccion, "cat": cat, "por": por,
                             "titulo": n["titulo"], "resumen": n.get("resumen", ""),
                             "importa": "", "fuente": n["medio"], "url": n["link"]})
    hn = [n["titulo"] for n in noticias if n["seccion"] == "honduras"]
    mundo = [n["titulo"] for n in noticias if n["seccion"] == "internacional"]
    return {
        "fecha": f"Resumen del {fecha_bonita(hoy).lower()}",
        "rapido": (hn[:2] + mundo[:1]) or ["Hoy no se pudieron leer los medios."],
        "noticias": noticias,
        "dato": dolar(),
        "quiz": armar_quiz(noticias) or [{"q": "¿Cuántas noticias hay hoy?", "ops": ["0", "1", "2"],
                                          "ok": 0, "exp": "Hoy no se pudieron leer los medios."}],
    }


def actualizar_pagina(datos, archivo="index.html"):
    if not os.path.exists(archivo):
        print(f"[aviso] no existe {archivo}; no se actualiza la página", file=sys.stderr)
        return
    pagina = open(archivo, encoding="utf-8").read()
    js = json.dumps(datos, ensure_ascii=False, indent=1).replace("</", "<\\/")
    nueva, n = re.subn(r'(<script id="news-data" type="application/json">).*?(</script>)',
                       lambda m: m.group(1) + "\n" + js + "\n" + m.group(2), pagina, flags=re.S)
    if n != 1:
        print("[aviso] no encontré el bloque news-data en la página", file=sys.stderr)
        return
    open(archivo, "w", encoding="utf-8").write(nueva)
    print(f"Página actualizada: {len(datos['noticias'])} noticias")


def url_pagina():
    """Dirección de GitHub Pages, ej. https://usuario.github.io/repositorio/"""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    dueno, nombre = repo.split("/", 1)
    return f"https://{dueno.lower()}.github.io/{nombre}/"


# ---------------------------------------------------------------------------
# MENSAJE Y ENVÍO
# ---------------------------------------------------------------------------

def fecha_bonita(d):
    return f"{DIAS[d.weekday()].capitalize()} {d.day} de {MESES[d.month - 1]} de {d.year}"


def armar_mensaje(secciones, ahora, pagina=""):
    hoy = ahora.astimezone(HN_TZ)
    partes = [f"📰 <b>Agencia de Noticias HN</b>\n<i>{fecha_bonita(hoy)}</i>"]
    for seccion, noticias in secciones.items():
        if not noticias:
            continue
        bloque = [f"\n<b>{TITULOS[seccion]}</b>"]
        for n in noticias:
            bloque.append(
                f"• <a href=\"{html.escape(n['link'], quote=True)}\">"
                f"{html.escape(n['titulo'])}</a> — <i>{html.escape(n['medio'])}</i>"
            )
        partes.append("\n".join(bloque))
    if len(partes) == 1:
        partes.append("\nHoy no se pudieron leer los medios. Intentaremos mañana. 🙏")
    if pagina:
        partes.append(f"\n🏢 <a href=\"{pagina}\">Ver la oficina y jugar el quiz</a>")
    partes.append("\n📨 Te lo trajo Amílcar")
    return "\n".join(partes)


def dividir(texto, limite=4000):
    """Telegram acepta máximo 4096 caracteres por mensaje."""
    trozos, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > limite:
            trozos.append(actual)
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        trozos.append(actual)
    return trozos


def enviar(token, chat_id, texto):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for trozo in dividir(texto):
        datos = json.dumps({
            "chat_id": chat_id,
            "text": trozo,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(url, data=datos,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.load(r)
            if not resp.get("ok"):
                raise RuntimeError(f"Telegram respondió: {resp}")


def main():
    ahora = datetime.now(timezone.utc)
    vistos = set()  # evita repetir la misma nota en las dos secciones
    secciones = {s: juntar(s, ahora, vistos) for s in MEDIOS}
    actualizar_pagina(datos_pagina(secciones, ahora))
    mensaje = armar_mensaje(secciones, ahora, url_pagina())

    if os.environ.get("DRY_RUN") == "1":
        print(mensaje)
        return

    # Telegram es opcional: solo se usa si configuras los dos secretos.
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        enviar(token, chat_id, mensaje)
        print(f"Mensaje enviado a Telegram ({chat_id})")
    else:
        print("Telegram no está configurado: solo se actualizó la página.")


if __name__ == "__main__":
    main()
