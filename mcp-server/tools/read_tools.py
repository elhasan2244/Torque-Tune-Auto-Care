try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP
from databases.db import get_connection

from app import mcp

@mcp.tool()
def search_spare_part(part_name :str):
    """
    Search for a spare part by its name.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM SpareParts WHERE part_name LIKE ?", (f"%{part_name}%",))
        results = cursor.fetchall()
    finally:
        conn.close()

    if not results:
        raise ValueError("No spare parts found with the given name.")
    else:
        return results

@mcp.tool()
def check_stock(part_id: int):
    """
    Check the stock level of a spare part by its ID.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT quantity FROM SpareParts WHERE id = ?", (part_id,))
        result = cursor.fetchone()
    finally:
        conn.close()

    if result:
        return {"part_id": part_id, "quantity": result[0]}
    else:
        raise ValueError("Part not found.")


@mcp.tool()
def suggest_alternative(part_id: int):
    """
    Suggest alternative spare parts.
    """

    conn = get_connection()
    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT s.part_name
            FROM AlternativeParts a
            JOIN SpareParts s
            ON a.alternative_part_id = s.id
            WHERE a.part_id = ?
        """, (part_id,))

        result = cursor.fetchall()
    finally:
        conn.close()

    if not result:
        raise ValueError("No alternative parts found for the given part ID.")
    else:
        return result
