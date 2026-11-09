# Archivo: models.py

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime, timezone
import uuid

def generar_uuid():
    return str(uuid.uuid4())

class Fuente(Base):
    __tablename__ = "fuentes"
    id_fuente = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    url_base = Column(String, nullable=False)
    activo = Column(Boolean, default=True)
    noticias = relationship("Noticia", back_populates="fuente")

class Categoria(Base):
    __tablename__ = "categorias"
    id_categoria = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    descripcion = Column(String)
    noticias = relationship("Noticia", back_populates="categoria")

class Noticia(Base):
    __tablename__ = "noticias"
    id_noticia = Column(String, primary_key=True, index=True, default=generar_uuid)
    titulo = Column(String, nullable=False)
    url_original = Column(String, nullable=False, unique=True)
    fecha_pub = Column(DateTime)
    
    # Contenidos y Metadatos Analíticos
    cuerpo_texto = Column(Text)
    resumen = Column(Text)
    imagen_url = Column(Text, nullable=True)
    autor = Column(String, default="Redacción")
    polaridad = Column(String, default="Neutral") # <-- NUEVA COLUMNA
    
    creado_en = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    id_fuente = Column(Integer, ForeignKey("fuentes.id_fuente"))
    id_categoria = Column(Integer, ForeignKey("categorias.id_categoria"))
    
    fuente = relationship("Fuente", back_populates="noticias")
    categoria = relationship("Categoria", back_populates="noticias")