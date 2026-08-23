try:
    from mcp.server.fastmcp import FastMCP, Context
except ImportError:
    from fastmcp import FastMCP, Context

from app import mcp
# Import the global memory manager
from app import memory_manager

from databases.db import get_connection
from auth.authorization import require_manager
# from validation.schemas import SearchKnowledgeBaseInput
from validation.validators import (
    authorize_update_inventory,
    validate_update_inventory,
    compute_new_quantity,
    ElicitationRequired,
    ValidationError,
    AuthorizationError,
)
from elicitation.elicitation import build_inventory_confirmation

from notifications import (
    inventory_updated,
    spare_part_added,
    spare_part_deleted,
)

from progress import report_inventory_progress
# from option_b_memory_example import load_memory_context, maybe_remember

# -----------------------------
# Helper Functions
# -----------------------------

def _ensure_non_negative(quantity: int):
    if quantity < 0:
        raise ValueError("Quantity cannot be negative.")


def _part_exists(cursor, part_id: int) -> bool:
    cursor.execute(
        "SELECT 1 FROM SpareParts WHERE id = ?",
        (part_id,)
    )
    return cursor.fetchone() is not None


def _part_number_exists(cursor, part_number: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM SpareParts WHERE part_number = ?",
        (part_number,)
    )
    return cursor.fetchone() is not None


def _category_exists(cursor, category_id: int) -> bool:
    cursor.execute(
        "SELECT 1 FROM Categories WHERE id = ?",
        (category_id,)
    )
    return cursor.fetchone() is not None


def _supplier_exists(cursor, supplier_id: int) -> bool:
    cursor.execute(
        "SELECT 1 FROM Suppliers WHERE id = ?",
        (supplier_id,)
    )
    return cursor.fetchone() is not None


def _resolve_user_role(cursor, user_id: int) -> str:
    """
    Look up a user's role server-side, the same pattern update_inventory
    already uses: authorization is based on the role stored in Users for
    this user_id, never a role the caller merely claims.
    """
    cursor.execute("SELECT role FROM Users WHERE id = ?", (user_id,))
    user_row = cursor.fetchone()
    if user_row is None:
        raise AuthorizationError(f"User {user_id} not found.")
    return user_row[0]


# -----------------------------
# Update Inventory
# -----------------------------

@mcp.tool()
async def update_inventory(
    part_id: int,
    action: str,
    quantity: int,
    reason: str,
    user_id: int,
    ctx: Context,
):
    """
    Adjust the stock quantity of a spare part. Manager role required.
    Large decreases or decreases that would zero out stock require
    human confirmation via elicitation before they are applied.
    """

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Authorization happens in the handler, against the role the server
        # looks up for user_id — never against a role the caller merely claims.
        cursor.execute("SELECT role FROM Users WHERE id = ?", (user_id,))
        user_row = cursor.fetchone()
        if user_row is None:
            raise AuthorizationError(f"User {user_id} not found.")
        authorize_update_inventory(user_row[0])

        cursor.execute(
            "SELECT quantity, status FROM SpareParts WHERE id = ?",
            (part_id,)
        )
        part_row = cursor.fetchone()
        if part_row is None:
            raise ValueError("Spare part not found.")

        current_quantity, part_status = part_row

        outcome = validate_update_inventory(
            action=action,
            quantity=quantity,
            current_quantity=current_quantity,
            part_status=part_status,
            reason=reason,
        )

        if isinstance(outcome, ElicitationRequired):
            confirmation = build_inventory_confirmation(
                part_id=part_id,
                old_quantity=outcome.proposed_old_quantity,
                new_quantity=outcome.proposed_new_quantity,
            )

            response = await ctx.elicit(
                message=confirmation["message"],
                schema={
                    "type": "object",
                    "properties": {"confirm": {"type": "boolean"}},
                    "required": ["confirm"],
                },
            )

            if response.action != "accept" or not response.data:
                return {
                    "success": False,
                    "message": "Inventory change cancelled — confirmation was not granted.",
                }

            new_quantity = outcome.proposed_new_quantity
        else:
            new_quantity = compute_new_quantity(action, quantity, current_quantity)

        cursor.execute(
            "UPDATE SpareParts SET quantity = ? WHERE id = ?",
            (new_quantity, part_id)
        )

        cursor.execute(
            """
            INSERT INTO InventoryLogs
            (part_id, user_id, old_quantity, new_quantity, action, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (part_id, user_id, current_quantity, new_quantity, action, reason)
        )

        conn.commit()

        # Update the scratchpad to mark the inventory update step as complete
        memory_manager.scratchpad.complete_step(
            step="update_inventory",
            result=f"Updated part {part_id} with action {action} by {quantity}.",
            result_key=f"inventory_update_{part_id}"
        )
        
        # Save this significant event in the episodic memory
        memory_manager.episodic.add_episode(
            event_type="inventory_updated",
            content={
                "part_id": part_id,
                "action": action,
                "quantity": quantity,
                "reason": reason,
                "user_id": user_id,
                "new_quantity": new_quantity
            },
            promotion_reason="Significant stock change executed by user."
        )

        return inventory_updated(part_id, new_quantity)

    finally:
        conn.close()


# -----------------------------
# Add Spare Part
# -----------------------------

@mcp.tool()
def add_spare_part(
    part_id: int | None,
    part_name: str,
    part_number: str,
    category_id: int,
    supplier_id: int,
    quantity: int,
    price: float,
    location: str,
    user_id: int,
    minimum_stock: int = 5,
    status: str = "active",
):
    """
    Add a new spare part to the inventory. Manager role required.

    part_number, category_id, supplier_id, price, and location are all
    required by the SpareParts schema (see databases/schema.sql) -- every
    field here maps directly to a NOT NULL column on that table.
    """

    # Cheap, DB-independent checks fail fast before opening a connection --
    # same fail-fast behavior the previous implementation had.
    _ensure_non_negative(quantity)

    if not part_name.strip():
        raise ValueError("Part name cannot be empty.")
    if not part_number.strip():
        raise ValueError("Part number cannot be empty.")
    if not location.strip():
        raise ValueError("Location cannot be empty.")
    if price < 0:
        raise ValueError("Price cannot be negative.")
    if minimum_stock < 0:
        raise ValueError("Minimum stock cannot be negative.")
    if status not in {"active", "discontinued"}:
        raise ValueError("Status must be 'active' or 'discontinued'.")

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Authorization happens in the handler, against the role the server
        # looks up for user_id — never against a role the caller merely claims.
        user_role = _resolve_user_role(cursor, user_id)
        require_manager(user_role)

        if not _category_exists(cursor, category_id):
            raise ValueError(f"Category {category_id} does not exist.")
        if not _supplier_exists(cursor, supplier_id):
            raise ValueError(f"Supplier {supplier_id} does not exist.")
        if _part_number_exists(cursor, part_number):
            raise ValueError(f"Part number '{part_number}' already exists.")

        # Insert with manually provided ID
        if part_id is not None:

            if _part_exists(cursor, part_id):
                raise ValueError("Spare part already exists.")

            cursor.execute(
                """
                INSERT INTO SpareParts
                (id, part_name, part_number, category_id, supplier_id, quantity, price, location, minimum_stock, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (part_id, part_name, part_number, category_id, supplier_id, quantity, price, location, minimum_stock, status)
            )

        # Let database generate the ID
        else:

            cursor.execute(
                """
                INSERT INTO SpareParts
                (part_name, part_number, category_id, supplier_id, quantity, price, location, minimum_stock, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (part_name, part_number, category_id, supplier_id, quantity, price, location, minimum_stock, status)
            )

            part_id = cursor.lastrowid

        conn.commit()

        # Update scratchpad
        memory_manager.scratchpad.complete_step(
            step="add_spare_part",
            result=f"Added new part: {part_name} with quantity {quantity}.",
            result_key=f"spare_part_added_{part_id}"
        )
        
        # Add to episodic memory
        memory_manager.episodic.add_episode(
            event_type="spare_part_added",
            content={
                "part_id": part_id,
                "part_name": part_name,
                "part_number": part_number,
                "quantity": quantity,
                "user_id": user_id,
                "user_role": user_role
            },
            promotion_reason="New spare part added to the inventory."
        )

        return spare_part_added(
            part_id,
            part_name
        )

    finally:
        conn.close()


# -----------------------------
# Delete Spare Part
# -----------------------------

@mcp.tool()
def delete_spare_part(
    part_id: int,
    user_id: int
):
    """
    Delete a spare part from the inventory. Manager role required.
    """

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Authorization happens in the handler, against the role the server
        # looks up for user_id — never against a role the caller merely claims.
        user_role = _resolve_user_role(cursor, user_id)
        require_manager(user_role)

        if not _part_exists(cursor, part_id):
            raise ValueError("Spare part not found.")

        cursor.execute(
            """
            DELETE FROM SpareParts
            WHERE id = ?
            """,
            (part_id,)
        )

        conn.commit()

        # Update scratchpad
        memory_manager.scratchpad.complete_step(
            step="delete_spare_part",
            result=f"Deleted part ID: {part_id}.",
            result_key=f"spare_part_deleted_{part_id}"
        )
        
        # Add to episodic memory
        memory_manager.episodic.add_episode(
            event_type="spare_part_deleted",
            content={
                "part_id": part_id,
                "user_id": user_id,
                "user_role": user_role
            },
            promotion_reason="Spare part permanently removed from the inventory."
        )

        return spare_part_deleted(part_id)

    finally:
        conn.close()


# -----------------------------
# Generate Inventory Report
# -----------------------------

@mcp.tool()
async def generate_inventory_report(ctx: Context):
    """
    Generate a summary report for the spare parts inventory
    while reporting progress to the client.
    """

    await report_inventory_progress(ctx, 0)

    conn = get_connection()
    cursor = conn.cursor()

    try:
        # Step 1: Read inventory
        await report_inventory_progress(ctx, 25)

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_parts,
                COALESCE(SUM(quantity), 0) AS total_quantity,
                COALESCE(AVG(price), 0) AS average_price
            FROM SpareParts
            """
        )

        report = cursor.fetchone()

        # Step 2: Calculate report
        await report_inventory_progress(ctx, 50)

        total_parts = report[0]
        total_quantity = report[1]
        average_price = round(report[2], 2)

        # Step 3: Prepare report
        await report_inventory_progress(ctx, 75)

        result = {
            "success": True,
            "total_parts": total_parts,
            "total_quantity": total_quantity,
            "average_price": average_price
        }        
        # Completed
        await report_inventory_progress(ctx, 100)

        # Update scratchpad
        memory_manager.scratchpad.complete_step(
            step="generate_inventory_report",
            result="Inventory report generated successfully.",
            result_key="latest_inventory_report"
        )
        
        # Add to episodic memory
        memory_manager.episodic.add_episode(
            event_type="inventory_report_generated",
            content=result,
            promotion_reason="User requested a full inventory summary report."
        )

        return result

    finally:
        conn.close()
        
        

# def handle_user_message(user_message: str, entity_id: str):
#     # Load relevant past memories for this entity
#     memory_context = load_memory_context(entity_id, user_message)

#     # Build prompt with memory
#     prompt = f"""
#     Previous relevant notes:
#     {memory_context}

#     User message:
#     {user_message}
#     """

#     # Generate reply from your agent/model
#     reply = generate_reply(prompt)

#     # Save important facts for future sessions
#     maybe_remember(user_message, entity_id)

#     return reply
