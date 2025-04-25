"""RAG (Retrieval Augmented Generation) using OpenAI and FAISS."""
import os
from typing import Dict, List, Optional, Any
from openai import OpenAI
from dotenv import load_dotenv
import threading
from concurrent.futures import ThreadPoolExecutor
from .embeddings import search_embeddings, search_all_documents

# Load environment variables
load_dotenv()

# Get OpenAI API key
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY environment variable is not set")

# Initialize OpenAI client
client = OpenAI(api_key=api_key)

# Default model for completions
COMPLETION_MODEL = "gpt-4.1-mini-2025-04-14"
# Model for query expansion (can use a smaller/faster model)
EXPANSION_MODEL = "gpt-4.1-mini-2025-04-14"

def format_context(chunks: List[Dict]) -> str:
    """Format retrieved chunks into a context string."""
    if not chunks:
        return ""
    
    formatted_chunks = []
    for i, chunk in enumerate(chunks):
        metadata = chunk.get('metadata', 'Unknown source')
        source = metadata.get('filename', 'Unknown source')
        formatted_chunks.append(f"[Chunk {i+1} - Source: {source}]\n{chunk['text']}\n")
    
    return "\n".join(formatted_chunks)

def expand_query(query: str, num_expansions: int = 3) -> List[str]:
    """Generate expanded queries to improve retrieval."""
    try:
        messages = [
            {"role": "system", "content": (
                "You are a query expansion assistant. Your task is to generate alternative "
                "Covered topics: key EU regulations like CSRD, Taxonomy, and ESRS, along with GHG Protocols"
                "(general, project-level, agriculture) and UN guidelines."
                "versions of the user's query that might retrieve additional relevant information. "
                "Generate semantically different but related queries that explore different aspects "
                "or phrasings of the same information need. Return ONLY a numbered list of queries, "
                "no explanations or other text. "
                "mix of German and English language"
            )},
            {"role": "user", "content": f"Original query: '{query}'\n\nGenerate {num_expansions} alternative queries."}
        ]
        
        response = client.chat.completions.create(
            model=EXPANSION_MODEL,
            messages=messages,
            temperature=0.7
        )
        expanded_text = response.choices[0].message.content.strip()
        
        # Parse the expanded queries from the response
        expanded_queries = []
        for line in expanded_text.split('\n'):
            # Remove numbered list formatting and any extra whitespace
            clean_line = line.strip()
            if clean_line:
                # Remove numbering (e.g., "1.", "2.", etc.)
                if clean_line[0].isdigit() and '.' in clean_line[:3]:
                    clean_line = clean_line.split('.', 1)[1].strip()
                # Remove quotes if present
                if clean_line.startswith('"') and clean_line.endswith('"'):
                    clean_line = clean_line[1:-1]
                if clean_line.startswith("'") and clean_line.endswith("'"):
                    clean_line = clean_line[1:-1]
                expanded_queries.append(clean_line)
        
        return expanded_queries[:num_expansions]  # Ensure we return at most num_expansions queries
    
    except Exception as e:
        print(f"Error in query expansion: {str(e)}")
        return []  # Return empty list if expansion fails

def deduplicate_chunks(chunks: List[Dict]) -> List[Dict]:
    """Remove duplicate chunks based on chunk_id."""
    unique_chunks = {}
    for chunk in chunks:
        chunk_id = chunk.get('chunk_id')
        if chunk_id and chunk_id not in unique_chunks:
            unique_chunks[chunk_id] = chunk
    
    return list(unique_chunks.values())

def search_with_query(q: str, top_k: int) -> List[Dict]:
    """Helper function to search documents with a query."""
    return search_all_documents(q, top_k)

def generate_answer(
    query: str,
    conversation_history: Optional[str] = None,
    top_k: int = 3,
    model: str = COMPLETION_MODEL,
    temperature: float = 0.0,
    meta_information: Optional[str] = None
) -> Dict[str, Any]:
    """Generate an answer using RAG."""
    try:
        # First, expand the query to improve retrieval
        expanded_queries = expand_query(query)
        
        # Search for relevant chunks across all documents
        all_chunks = []
        for expanded_query in expanded_queries:
            chunks = search_all_documents(expanded_query, top_k)
            all_chunks.extend(chunks)
        
        # Remove duplicates and sort by score
        unique_chunks = []
        seen_texts = set()
        for chunk in sorted(all_chunks, key=lambda x: x["score"]):
            if chunk["text"] not in seen_texts:
                unique_chunks.append(chunk)
                seen_texts.add(chunk["text"])
        
        # Format context from chunks
        context = format_context(unique_chunks[:top_k])
        print(context)
        
        # Build the prompt
        system_prompt = """You are an expert assistant specialized in sustainability reporting, regulations, and technical standards.

CRITICAL INSTRUCTIONS:
1. ONLY use information directly from the provided context documents
2. Do NOT use prior knowledge that isn't in the provided documents
3. If the documents don't contain sufficient information, clearly state this limitation
4. ALWAYS cite sources by their exact designation and date in parentheses after relevant statements
5. NEVER make up citations or references
6. If you're asked about something not covered in the documents, say "I don't have specific information about that in my documents"

IMPORTANT ABOUT DOCUMENTS:
- The source documents shown after your response MUST match what you actually used to answer
- If the documents don't contain information on the specific topic, acknowledge this limitation
- NEVER pretend to know something if it's not in the documents
- Prioritize official EU regulation documents over guidance documents
- For regulation questions, cite specific article numbers when available

FORMATTING AND CONTENT:
- Structure your responses with clear headings and bullet points when appropriate
- Use plain language to explain complex concepts
- Provide comprehensive answers that address all aspects of the question
- Include specific dates, numbers, and metrics from the documents when relevant
- When appropriate, organize information chronologically or by relevance

CITATION FORMAT:
- Citation format: (Document-Designation-Date) - e.g., (CSRD-2022/2464-2022-12-14)
- Include the citation immediately after the information it supports
- For general information from multiple sources, cite all relevant documents
- Never invent citations or reference documents not in the provided context"""

        # Add meta information if available
        if meta_information and meta_information.strip():
            system_prompt += f"\n\nAdditional context from the user:\n{meta_information}"
        
        # Add conversation history if available
        if conversation_history:
            system_prompt += f"\n\nPrevious conversation:\n{conversation_history}\n\nPlease consider the previous conversation when answering the current question."
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "system", "content": f"Context:\n{context}"},
            {"role": "user", "content": query}
        ]
        
        # Generate response
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature
        )
        
        return {
            "answer": response.choices[0].message.content,
            "chunks": unique_chunks[:top_k],
            "expanded_queries": expanded_queries,
            "sources": [chunk.get('source', 'Unknown source') for chunk in unique_chunks[:top_k]],
            "success": True
        }
        
    except Exception as e:
        print(f"Error generating answer: {e}")
        return {
            "answer": "I apologize, but I encountered an error while processing your request.",
            "chunks": [],
            "expanded_queries": [],
            "sources": [],
            "success": False
        } 

