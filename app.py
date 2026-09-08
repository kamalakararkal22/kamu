"""
RAG (Retrieval-Augmented Generation) service over a Knowledge Transfer (KT)
document, using Gemini for embeddings + chat and an in-memory vector store.

Setup:
    pip install -r requirements.txt
    export GEMINI_API_KEY="your-key-here"

Run locally:
    uvicorn app:app --reload

Render start command:
    uvicorn app:app --host 0.0.0.0 --port $PORT
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

KB_PATH = Path(__file__).parent / "knowledge_base.md"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
TOP_K = 4

STATE = {"vector_store": None, "llm": None}


def _get_api_key() -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise ValueError(
            "GEMINI_API_KEY not found. Set it as an environment variable "
            "(on Render: Environment tab) or in a local .env file."
        )
    return key


def _build_index() -> InMemoryVectorStore:
    if not KB_PATH.exists():
        raise FileNotFoundError(f"Knowledge base file not found: {KB_PATH}")

    text = KB_PATH.read_text(encoding="utf-8")
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    chunks = splitter.split_text(text)
    docs = [Document(page_content=c, metadata={"chunk_id": i}) for i, c in enumerate(chunks)]

    embeddings = GoogleGenerativeAIEmbeddings(
        model="gemini-embedding-001", google_api_key=STATE["api_key"]
    )
    return InMemoryVectorStore.from_documents(docs, embeddings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE["api_key"] = _get_api_key()
    STATE["llm"] = ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite", google_api_key=STATE["api_key"]
    )
    STATE["vector_store"] = _build_index()
    yield
    STATE.clear()


app = FastAPI(title="KT RAG Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask")
def ask(req: AskRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question cannot be empty")

    vector_store = STATE.get("vector_store")
    llm = STATE.get("llm")
    if vector_store is None or llm is None:
        raise HTTPException(status_code=503, detail="RAG index not ready")

    results = vector_store.similarity_search(req.question, k=TOP_K)
    context = "\n\n---\n\n".join(doc.page_content for doc in results)

    prompt = (
        "You are an assistant answering questions using ONLY the context "
        "below, which comes from an internal Knowledge Transfer document. "
        "If the answer isn't in the context, say you don't know.\n\n"
        f"Context:\n{context}\n\n"
        f"Question: {req.question}\n\n"
        "Answer:"
    )
    response = llm.invoke(prompt)
    answer = response.content if hasattr(response, "content") else str(response)

    return {
        "question": req.question,
        "answer": answer,
        "sources": [{"chunk_id": doc.metadata.get("chunk_id"), "text": doc.page_content} for doc in results],
    }