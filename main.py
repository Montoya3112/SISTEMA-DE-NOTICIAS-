# Archivo: main.py
import os
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, joinedload
from dotenv import load_dotenv

from database import get_db
import models
from scraper import iniciar_scraping

load_dotenv()
app = FastAPI(title="Sistema Inteligente de Noticias", version="4.0.0")

# NUEVO: Montar la carpeta estática para la imagen de fondo
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse, tags=["Interfaz Web"])
def mostrar_dashboard(request: Request, db: Session = Depends(get_db)):
    noticias = (
        db.query(models.Noticia)
        .options(joinedload(models.Noticia.categoria), joinedload(models.Noticia.fuente))
        .order_by(models.Noticia.creado_en.desc())
        .limit(100)
        .all()
    )

    noticias_recomendadas = sorted(
        [n for n in noticias if n.cuerpo_texto and len(n.cuerpo_texto) > 800],
        key=lambda x: len(x.cuerpo_texto), 
        reverse=True
    )[:15]

    return templates.TemplateResponse(
        request=request,
        name="index.html", 
        context={
            "request": request, 
            "titulo": "Sistema Inteligente de Noticias",
            "noticias": noticias,
            "recomendadas": noticias_recomendadas,
            "supa_url": os.getenv("SUPABASE_URL"),
            "supa_key": os.getenv("SUPABASE_KEY")
        }
    )

@app.post("/ejecutar-scraper")
def disparar_extraccion_manual():
    resultado = iniciar_scraping()
    if resultado["estado"] == "error": raise HTTPException(500, resultado['detalle'])
    return resultado

@app.delete("/noticias/{id_noticia}")
def eliminar_noticia(id_noticia: str, db: Session = Depends(get_db)):
    noticia = db.query(models.Noticia).filter(models.Noticia.id_noticia == id_noticia).first()
    if not noticia: raise HTTPException(404)
    db.delete(noticia)
    db.commit()
    return {"estado": "éxito"}