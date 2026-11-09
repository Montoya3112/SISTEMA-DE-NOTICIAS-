# Archivo: scraper.py
# Dependencias nuevas (para saltar Cloudflare sin Selenium):
#   pip install curl_cffi
import re
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

from database import SessionLocal
from models import Noticia, Fuente, Categoria

try:
    from curl_cffi import requests as cffi_requests  # imita la huella TLS de Chrome
except ImportError:
    cffi_requests = None

MAX_POR_FUENTE = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

FUENTES_CONFIG = [
    # NACIONALES
    {"nombre": "Google News Nacional", "url": "https://news.google.com/rss/search?q=M%C3%A9xico+Nacional&hl=es-419&gl=MX&ceid=MX:es-419", "base": "https://news.google.com", "cat": "Nacional", "agregador": True},
    {"nombre": "La Jornada", "url": "https://www.jornada.com.mx/rss/edicion.xml", "base": "https://www.jornada.com.mx", "cat": "Nacional"},
    {"nombre": "Expansión Nacional", "url": "https://expansion.mx/rss/nacional", "base": "https://expansion.mx", "cat": "Nacional"},
    # ECONOMÍA
    {"nombre": "El Financiero", "url": "https://www.elfinanciero.com.mx/arc/outboundfeeds/rss/?outputType=xml", "base": "https://www.elfinanciero.com.mx", "cat": "Economía"},
    {"nombre": "Forbes México", "url": "https://www.forbes.com.mx/feed/", "base": "https://www.forbes.com.mx", "cat": "Economía"},
    {"nombre": "Expansión", "url": "https://expansion.mx/rss", "base": "https://expansion.mx", "cat": "Economía"},
    # INTERNACIONAL
    {"nombre": "BBC Mundo", "url": "https://feeds.bbci.co.uk/mundo/rss.xml", "base": "https://www.bbc.com/mundo", "cat": "Internacional"},
    {"nombre": "El País", "url": "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada", "base": "https://elpais.com", "cat": "Internacional"},
    # TECNOLOGÍA
    {"nombre": "Xataka", "url": "https://www.xataka.com/index.xml", "base": "https://www.xataka.com", "cat": "Tecnología"},
    {"nombre": "Genbeta", "url": "https://www.genbeta.com/index.xml", "base": "https://www.genbeta.com", "cat": "Tecnología"},
]

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


# =====================================================================
# 1. SENTIMIENTO: léxico + palabras completas + negación + título x2
# =====================================================================
def _norm(texto):
    """Minúsculas y sin acentos, para comparar 'pérdida' == 'perdida'."""
    t = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


_POS = {_norm(p) for p in [
    "crecimiento", "éxito", "avance", "avances", "ganancia", "ganancias", "positivo", "mejora",
    "mejoras", "mejor", "acuerdo", "acuerdos", "solución", "beneficio", "beneficios", "paz",
    "inversión", "inversiones", "logro", "logros", "apoyo", "estabilidad", "alza", "récord",
    "sube", "subió", "recuperación", "impulso", "ganan", "gana", "fortalece", "oportunidad",
    "oportunidades", "optimismo", "rescate", "alianza", "desarrollo"]}
_NEG = {_norm(p) for p in [
    "crisis", "caída", "pérdida", "pérdidas", "violencia", "muerte", "muertes", "muerto",
    "muertos", "conflicto", "negativo", "problema", "problemas", "riesgo", "riesgos", "deuda",
    "crimen", "asesinato", "asesinatos", "robo", "guerra", "tensión", "desempleo", "inflación",
    "baja", "desastre", "desplome", "peor", "preocupación", "fraude", "corrupción", "ataque",
    "ataques", "cae", "cayó", "retroceso", "amenaza", "peligro", "choque", "incertidumbre",
    "quiebra"]}
_NEGADORES = {_norm(p) for p in [
    "no", "sin", "nunca", "ni", "evita", "evitan", "evitó", "evitar", "evitaron",
    "descarta", "descartan", "descartó", "libra", "superó", "supera"]}
_TOKEN = re.compile(r"[a-zñ]+")


def analizar_polaridad(texto, titulo=""):
    """Léxico por palabras completas (no subcadenas: 'baja' ya no dispara con 'trabaja'),
    con inversión por negación en una ventana de 3 palabras y el título con doble peso."""
    if not texto and not titulo:
        return "Neutral"
    tokens = _TOKEN.findall(_norm(f"{titulo} {titulo} {texto or ''}"))
    score = 0
    for i, tok in enumerate(tokens):
        val = 1 if tok in _POS else -1 if tok in _NEG else 0
        if not val:
            continue
        if any(w in _NEGADORES for w in tokens[max(0, i - 3):i]):
            val = -val
        score += val
    if score >= 2:
        return "Positivo"
    if score <= -2:
        return "Negativo"
    return "Neutral"


# =====================================================================
# 2. RED: cliente HTTP con huella de navegador real (fallback a requests)
# =====================================================================
def http_get(url, timeout=12):
    headers = {"Accept-Language": "es-MX,es;q=0.9", "Referer": "https://www.google.com/"}
    try:
        if cffi_requests:
            r = cffi_requests.get(url, impersonate="chrome", headers=headers, timeout=timeout)
        else:
            headers["User-Agent"] = UA
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            r = requests.get(url, headers=headers, timeout=timeout)
        return r if r.status_code == 200 else None
    except Exception:
        return None


# =====================================================================
# 3. LIMPIEZA DE TEXTO Y AUTOR
# =====================================================================
def html_a_parrafos(html, min_len=0):
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "button"]):
        tag.decompose()
    parrafos = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
    parrafos = [p for p in parrafos if len(p) >= min_len]
    if parrafos:
        return "\n\n".join(parrafos)
    return soup.get_text(" ", strip=True) if min_len == 0 else ""


_AUTORES_GENERICOS = {_norm(x) for x in [
    "redacción", "redaccion web", "staff", "admin", "editor", "equipo editorial", "agencias", "notimex"]}


def limpiar_autor(raw, fuente):
    """Devuelve un autor válido o None (nunca basura ni el nombre del periódico)."""
    if not raw:
        return None
    a = re.split(r"[|•·\n]", raw.strip())[0]
    a = re.sub(r"^\s*(por|by|texto:)\s+", "", a, flags=re.I)
    a = re.sub(r"\s+", " ", a).strip(" -–—,:")
    if not (3 <= len(a) <= 60) or "http" in a.lower() or any(c.isdigit() for c in a):
        return None
    n = _norm(a)
    if n in _AUTORES_GENERICOS or n == _norm(fuente):
        return None
    return a


def generar_resumen(texto_completo):
    if not texto_completo:
        return "Contenido no disponible para lectura rápida."
    parrafos = texto_completo.split("\n\n")
    if len(parrafos) >= 2:
        return "\n\n".join(parrafos[:2]) + "\n\n[Sigue leyendo en el documento...]"
    return texto_completo[:400] + "..."


# =====================================================================
# 4. DESCARGA DE FEEDS (en paralelo)
# =====================================================================
def _parsear_item(item, cfg):
    def txt(tag, ns=None):
        e = item.find(tag, ns) if ns else item.find(tag)
        return e.text.strip() if (e is not None and e.text) else ""

    enlace = txt("link")
    if not enlace:
        return None

    autor_rss = txt("dc:creator", NS) or txt("author")
    if not autor_rss and cfg.get("agregador"):
        autor_rss = txt("source")

    imagen = None
    enc = item.find("enclosure")
    if enc is not None and "url" in enc.attrib and "image" in enc.attrib.get("type", "image"):
        imagen = enc.attrib["url"]
    if not imagen:
        mc = item.find("media:content", NS)
        if mc is not None and "url" in mc.attrib:
            imagen = mc.attrib["url"]
    if not imagen:
        mt = item.find("media:thumbnail", NS)
        if mt is not None and "url" in mt.attrib:
            imagen = mt.attrib["url"]

    # Fecha REAL de publicación (antes se usaba la fecha del scraping)
    fecha = datetime.now(timezone.utc)
    pub = txt("pubDate")
    if pub:
        try:
            f = parsedate_to_datetime(pub)
            fecha = f if f.tzinfo else f.replace(tzinfo=timezone.utc)
        except Exception:
            pass

    return {
        "titulo": txt("title") or "Sin título",
        "url": enlace,
        "desc": txt("description"),
        "content": txt("content:encoded", NS),  # texto completo sin tocar el sitio
        "autor_rss": autor_rss,
        "imagen_rss": imagen,
        "fecha": fecha,
        "cfg": cfg,
    }


def _descargar_feed(cfg):
    r = http_get(cfg["url"])
    if r is None:
        return cfg, None
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return cfg, None
    items = []
    for it in root.findall(".//item")[:MAX_POR_FUENTE]:
        parsed = _parsear_item(it, cfg)
        if parsed:
            items.append(parsed)
    return cfg, items


# =====================================================================
# 5. ENRIQUECIMIENTO: solo visita la página si falta algo
# =====================================================================
def _enriquecer(item):
    fuente = item["cfg"]["nombre"]
    es_agregador = item["cfg"].get("agregador", False)

    texto = max(html_a_parrafos(item["content"]), html_a_parrafos(item["desc"]), key=len)
    imagen = item["imagen_rss"]
    autor = limpiar_autor(item["autor_rss"], fuente)

    # Google News redirige con JavaScript: no tiene sentido pedir la página.
    if not es_agregador and (len(texto) < 300 or not imagen or not autor):
        r = http_get(item["url"])
        if r is not None:
            soup = BeautifulSoup(r.content, "html.parser")

            if not imagen:
                og = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
                if og and og.get("content"):
                    imagen = og["content"]

            if not autor:
                meta = soup.find("meta", attrs={"name": "author"}) or soup.find("meta", property="article:author")
                if meta and meta.get("content"):
                    autor = limpiar_autor(meta["content"], fuente)
                if not autor:
                    span = soup.find(class_=re.compile(r"author|autor|byline", re.I))
                    if span:
                        autor = limpiar_autor(span.get_text(" ", strip=True), fuente)

            if len(texto) < 300:
                real = html_a_parrafos(str(soup), min_len=80)
                if len(real) > len(texto):
                    texto = real

    return texto, imagen, autor or f"Redacción {fuente}"


# =====================================================================
# 6. ORQUESTADOR
# =====================================================================
def iniciar_scraping():
    db = SessionLocal()
    try:
        # Fase 1: todos los feeds a la vez
        with ThreadPoolExecutor(max_workers=8) as pool:
            resultados = list(pool.map(_descargar_feed, FUENTES_CONFIG))

        fallidas = [cfg["nombre"] for cfg, items in resultados if items is None]
        pendientes, vistos = [], set()
        for _, items in resultados:
            for it in items or []:
                if it["url"] not in vistos:
                    vistos.add(it["url"])
                    pendientes.append(it)

        # Fase 2: descartar lo que ya existe con UNA sola consulta
        existentes = set()
        if pendientes:
            filas = db.query(Noticia.url_original).filter(
                Noticia.url_original.in_([i["url"] for i in pendientes])).all()
            existentes = {u for (u,) in filas}
        nuevos = [i for i in pendientes if i["url"] not in existentes]

        # Fase 3: enriquecer en paralelo (solo red; la BD NO es thread-safe)
        with ThreadPoolExecutor(max_workers=10) as pool:
            enriquecidos = list(pool.map(_enriquecer, nuevos))

        # Fase 4: escribir en la BD desde el hilo principal
        fuentes_cache, cats_cache = {}, {}
        for item, (texto, imagen, autor) in zip(nuevos, enriquecidos):
            cfg = item["cfg"]

            if cfg["nombre"] not in fuentes_cache:
                f = db.query(Fuente).filter(Fuente.nombre == cfg["nombre"]).first()
                if not f:
                    f = Fuente(nombre=cfg["nombre"], url_base=cfg["base"], activo=True)
                    db.add(f)
                    db.flush()
                fuentes_cache[cfg["nombre"]] = f
            if cfg["cat"] not in cats_cache:
                c = db.query(Categoria).filter(Categoria.nombre == cfg["cat"]).first()
                if not c:
                    c = Categoria(nombre=cfg["cat"], descripcion="Indexado automático")
                    db.add(c)
                    db.flush()
                cats_cache[cfg["cat"]] = c

            db.add(Noticia(
                titulo=item["titulo"],
                url_original=item["url"],
                fecha_pub=item["fecha"],
                cuerpo_texto=texto,
                resumen=generar_resumen(texto),
                imagen_url=imagen,
                autor=autor[:250],
                polaridad=analizar_polaridad(texto, item["titulo"]),
                id_fuente=fuentes_cache[cfg["nombre"]].id_fuente,
                id_categoria=cats_cache[cfg["cat"]].id_categoria,
            ))
        db.commit()

        detalle = f"Extracción exitosa. {len(nuevos)} documentos nuevos indexados."
        if fallidas:
            detalle += f" Fuentes sin respuesta: {', '.join(fallidas)}."
        return {"estado": "éxito", "detalle": detalle}
    except Exception as e:
        db.rollback()
        return {"estado": "error", "detalle": f"Error del motor: {str(e)}"}
    finally:
        db.close()