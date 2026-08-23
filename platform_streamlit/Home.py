import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

st.set_page_config(page_title="Torque Tune Platform", layout="wide")
st.title("Torque Tune Auto Care — Platform")

with st.spinner("جاري تحميل الـ state_graph..."):
    from state_graph.bootstrap import ensure_wired  # noqa: F401

st.success("state_graph اتحمّل بنجاح")

AGENTS = [
    {"id": "memory_rag", "name": "Memory / RAG Agent", "kind": "chat"},
    {"id": "planning", "name": "Planning Agent", "kind": "chat"},
    {"id": "purchase_order", "name": "Purchase Order", "kind": "state_graph"},
    {"id": "inventory_approval", "name": "Inventory Approval", "kind": "state_graph"},
    {"id": "warranty", "name": "Warranty Claim", "kind": "state_graph"},
]

st.subheader("Agents")
for a in AGENTS:
    st.write(f"- **{a['name']}** ({a['kind']})")
    st.divider()
st.subheader("جرب اسأل الـ Memory/RAG Agent")

question = st.text_input("اكتب سؤالك (مثال: هل القطعة دي لسه تحت الضمان؟)")

if st.button("إرسال") and question:
    sys.path.insert(0, str(ROOT / "mcp-server"))
    sys.path.insert(0, str(ROOT / "agent"))
    import tool_registry
    from client import is_planning_request

    if is_planning_request(question):
        st.warning(
            "السؤال ده متعلق بتوفير قطع غيار / إصلاح، وده بيحتاج "
            "job request منظم (job_id + required_parts) مش نص حر — "
            "الـ Planning Agent مبني على `planning/fulfillment_planning.py` "
            "وبياخد مدخلات structured، مش سؤال نصي زي RAG."
        )
    else:
        allowed = tool_registry.enabled_tools_for_agent("memory_rag")
        if "search_company_knowledge" not in allowed:
            st.error("أداة search_company_knowledge معطّلة لـ memory_rag من صفحة MCP Tools — فعّلها الأول")
        else:
            with st.spinner("بيدوّر في قاعدة المعرفة..."):
                import server  # noqa: F401  ensures tools are registered
                from app import mcp
                search_company_knowledge = mcp._tools["search_company_knowledge"]
                result = search_company_knowledge(question=question)

            st.write("**الرد:**", result["answer"])
            st.write("**مؤكد من مصدر حقيقي (grounded):**", result["grounded"])
            if result["sources"]:
                st.write("**المصادر:**")
                for s in result["sources"]:
                    st.write(f"- {s['document']} — {s['section']}")
                    st.divider()
st.subheader("MCP Tools — إدارة الأدوات لكل Agent")

sys.path.insert(0, str(ROOT / "mcp-server"))
import tool_registry

selected_agent = st.selectbox("اختار الـ Agent", tool_registry.list_agents())

tools_for_agent = tool_registry.list_tools(selected_agent)

for t in tools_for_agent:
    col1, col2 = st.columns([3, 1])
    with col1:
        st.write(t["tool"])
    with col2:
        new_val = st.checkbox("مفعّلة", value=t["enabled"], key=f"{selected_agent}-{t['tool']}")
        if new_val != t["enabled"]:
            tool_registry.set_tool_enabled(selected_agent, t["tool"], new_val)
            st.rerun()
            st.divider()
st.subheader("RAG Documents — إدارة مستندات قاعدة المعرفة")

sys.path.insert(0, str(ROOT / "mcp-server" / "rag"))
import registry as rag_registry

docs = rag_registry.list_documents()
for d in docs:
    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        st.write(f"**{d['name']}** ({d['doc_type']})")
    with col2:
        st.write(f"{d['size_bytes']} bytes")
    with col3:
        if d["editable"]:
            if st.button("حذف", key=f"del-{d['name']}"):
                rag_registry.remove_document(d["name"])
                st.warning("اتحذف — أعد تشغيل السيرفر عشان الفهرس يتحدث")
                st.rerun()

st.write("---")
st.write("**إضافة مستند جديد**")
new_name = st.text_input("اسم الملف (لازم ينتهي بـ .md)", key="new_doc_name")
new_content = st.text_area("محتوى المستند (Markdown، استخدم ## للعناوين)", key="new_doc_content")
if st.button("إضافة المستند"):
    rag_registry.add_document(new_name, new_content)
    st.success("اتضاف — أعد تشغيل السيرفر عشان الفهرس يتحدث")
    st.rerun()
    st.divider()
st.subheader("Inventory Approval — تشغيل الـ state graph")

from state_graph.graphs.inventory_approval_graph import build_graph as build_inv_graph
from state_graph.graphs.inventory_approval_graph import record_manager_decision as inv_decide
from state_graph.checkpointer import Checkpointer

thread_id_input = st.text_input("Thread ID (اسم فريد للطلب)", value="ui-inventory-1")

col1, col2 = st.columns(2)
with col1:
    part_id = st.number_input("Part ID", value=2, step=1)
    quantity = st.number_input("Quantity", value=2, step=1)
with col2:
    action = st.selectbox("Action", ["decrease", "increase"])
    reason = st.text_input("Reason", value="Sold to customer #4471")

if st.button("ابدأ الطلب"):
    compiled = build_inv_graph().compile()
    try:
        result = compiled.invoke(thread_id_input, {
            "part_id": int(part_id), "action": action,
            "quantity": int(quantity), "reason": reason, "user_id": 2,
        })
        st.session_state["inv_thread"] = thread_id_input
        st.write(f"status: **{result.status}**  node: {result.node_name}")
        if result.status == "paused_hitl":
            st.warning(f"الطلب محتاج موافقة مدير — sensitive={result.state.get('sensitive')}")
    except ValueError as e:
        st.error(str(e))

if st.session_state.get("inv_thread"):
    st.write("---")
    st.write(f"**Thread الحالي:** {st.session_state['inv_thread']}")
    cp = Checkpointer().latest(st.session_state["inv_thread"])
    if cp:
        st.write(f"آخر حالة: **{cp.status}** عند node `{cp.node_name}`")
        if cp.status == "paused_hitl":
            c1, c2 = st.columns(2)
            if c1.button("موافقة المدير ✅"):
                r = inv_decide(st.session_state["inv_thread"], approved=True)
                st.write(f"status={r.status} node={r.node_name}")
                st.rerun()
            if c2.button("رفض المدير ❌"):
                r = inv_decide(st.session_state["inv_thread"], approved=False)
                st.write(f"status={r.status} node={r.node_name}")
                st.rerun()
                st.divider()
st.subheader("Tickets — التذاكر المفتوحة")

from state_graph.tickets import list_tickets, resolve_ticket, mark_investigating

status_filter = st.selectbox("فلترة حسب الحالة", ["الكل", "open", "investigating", "resolved"])
tickets = list_tickets(status=None if status_filter == "الكل" else status_filter)

if not tickets:
    st.info("مفيش تذاكر حاليًا")

for t in tickets:
    with st.expander(f"#{t.id} — {t.graph_name} / {t.node_name} — {t.status}"):
        st.write(f"**النوع:** {t.error_type}")
        st.write(f"**الرسالة:** {t.error_message}")
        st.write(f"**Thread ID:** {t.thread_id}")
        st.write(f"**اتعملت:** {t.created_at}")

        if t.status == "open":
            if st.button("ابدأ التحقيق 🔎", key=f"inv-{t.id}"):
                mark_investigating(t.id)
                st.rerun()

        if t.status in ("open", "investigating"):
            note = st.text_input("ملاحظة الحل", key=f"note-{t.id}")
            if st.button("حل التذكرة ✅", key=f"resolve-{t.id}"):
                resolve_ticket(t.id, note or "resolved via platform")
                st.success("اتحلت — دلوقتي تقدر تستأنف الـ thread من زر الـ resume في صفحة الـ graph بتاعه")
                st.rerun()
                st.divider()
st.subheader("Purchase Order — تشغيل الـ state graph")

from state_graph.graphs.purchase_order_graph import build_graph as build_po_graph
from state_graph.graphs.purchase_order_graph import record_manager_decision as po_decide_manager
from state_graph.graphs.purchase_order_graph import record_supplier_reply as po_decide_supplier

po_thread_id = st.text_input("Thread ID", value="ui-po-1", key="po_thread_id")

if st.button("ابدأ أمر الشراء"):
    compiled = build_po_graph().compile()
    try:
        result = compiled.invoke(po_thread_id, {"user_id": 2})
        st.session_state["po_thread"] = po_thread_id
        st.write(f"status: **{result.status}**  node: {result.node_name}")
        if result.status == "paused_hitl":
            batch = result.state.get("current_batch", {})
            st.warning(f"محتاج موافقة مدير: PO بقيمة {batch.get('total_cost')} للمورد {batch.get('supplier_name')}")
    except ValueError as e:
        st.error(str(e))

if st.session_state.get("po_thread"):
    st.write("---")
    st.write(f"**Thread الحالي:** {st.session_state['po_thread']}")
    cp = Checkpointer().latest(st.session_state["po_thread"])
    if cp:
        st.write(f"آخر حالة: **{cp.status}** عند node `{cp.node_name}`")

        if cp.status == "paused_hitl":
            c1, c2 = st.columns(2)
            if c1.button("موافقة المدير على الـ PO ✅"):
                r = po_decide_manager(st.session_state["po_thread"], True)
                st.write(f"status={r.status} node={r.node_name}")
                st.rerun()
            if c2.button("رفض المدير ❌", key="po_reject"):
                r = po_decide_manager(st.session_state["po_thread"], False)
                st.write(f"status={r.status} node={r.node_name}")
                st.rerun()

        if cp.status == "paused_external":
            st.write("**الطلب اتبعت للمورد وبينتظر رد فعلي**")
            note = st.text_input("ملاحظة المورد", value="Ships in 5 business days", key="po_supplier_note")
            c1, c2 = st.columns(2)
            if c1.button("المورد أكّد ✅", key="po_confirm"):
                r = po_decide_supplier(st.session_state["po_thread"], "confirmed", note=note)
                st.write(f"status={r.status} node={r.node_name}")
                st.rerun()
            if c2.button("المورد رفض ❌", key="po_reject_supplier"):
                r = po_decide_supplier(st.session_state["po_thread"], "rejected", note=note)
                st.write(f"status={r.status} node={r.node_name}")
                st.rerun()

        if cp.status == "completed":
            st.success("PO اكتمل بنجاح")
            st.write(cp.state.get("batch_results"))
            st.divider()
st.subheader("Warranty Claim — تشغيل الـ state graph")

from state_graph.graphs.warranty_graph import build_graph as build_warranty_graph
from state_graph.graphs.warranty_graph import record_manager_decision as warranty_decide_manager
from state_graph.graphs.warranty_graph import record_supplier_reply as warranty_decide_supplier

w_thread_id = st.text_input("Thread ID", value="ui-warranty-1", key="w_thread_id")

col1, col2 = st.columns(2)
with col1:
    w_part_id = st.number_input("Part ID", value=4, step=1, key="w_part_id")
    w_log_id = st.number_input("Inventory Log ID", value=1, step=1, key="w_log_id")
with col2:
    w_reason = st.text_input("سبب المطالبة", value="manufacturing defect in caliper mount, not wear", key="w_reason")

if st.button("ابدأ مطالبة الضمان"):
    compiled = build_warranty_graph().compile()
    try:
        result = compiled.invoke(w_thread_id, {
            "part_id": int(w_part_id), "user_id": 2,
            "inventory_log_id": int(w_log_id), "claim_reason": w_reason,
        })
        st.session_state["w_thread"] = w_thread_id
        st.write(f"status: **{result.status}**  node: {result.node_name}")
    except ValueError as e:
        st.error(str(e))

if st.session_state.get("w_thread"):
    st.write("---")
    st.write(f"**Thread الحالي:** {st.session_state['w_thread']}")
    cp = Checkpointer().latest(st.session_state["w_thread"])
    if cp:
        st.write(f"آخر حالة: **{cp.status}** عند node `{cp.node_name}`")

        if cp.status == "paused_external":
            st.write("**بينتظر رد المورد**")
            w_note = st.text_input("ملاحظة المورد", value="Photos inconclusive", key="w_supplier_note")
            c1, c2 = st.columns(2)
            if c1.button("المورد وافق ✅", key="w_approve"):
                r = warranty_decide_supplier(st.session_state["w_thread"], "approved", note=w_note)
                st.write(f"status={r.status} node={r.node_name}")
                if r.state.get("appeal_argument"):
                    st.info(f"Tree-of-Thoughts appeal: {r.state['appeal_argument']}")
                st.rerun()
            if c2.button("المورد رفض ❌", key="w_reject"):
                r = warranty_decide_supplier(st.session_state["w_thread"], "rejected", note=w_note)
                st.write(f"status={r.status} node={r.node_name}")
                if r.state.get("appeal_argument"):
                    st.info(f"Tree-of-Thoughts appeal: {r.state['appeal_argument']}")
                st.rerun()

        if cp.status == "paused_hitl":
            st.write("**الاستئناف محتاج موافقة مدير**")
            c1, c2 = st.columns(2)
            if c1.button("موافقة المدير ✅", key="w_mgr_approve"):
                r = warranty_decide_manager(st.session_state["w_thread"], True)
                st.write(f"status={r.status} node={r.node_name}")
                st.rerun()
            if c2.button("رفض المدير ❌", key="w_mgr_reject"):
                r = warranty_decide_manager(st.session_state["w_thread"], False)
                st.write(f"status={r.status} node={r.node_name}")
                st.rerun()

        if cp.status == "completed":
            st.success(f"النتيجة النهائية: {cp.state.get('final_status')}")
            st.divider()
st.subheader("HITL Tasks — كل الطلبات المعلقة عبر التلات Graphs")

from state_graph.checkpointer import Checkpointer as CP

hitl_filter = st.selectbox(
    "فلترة",
    ["paused_hitl فقط", "paused_external فقط", "الكل (paused_hitl + paused_external)"],
    key="hitl_filter",
)

if hitl_filter == "paused_hitl فقط":
    statuses = ["paused_hitl"]
elif hitl_filter == "paused_external فقط":
    statuses = ["paused_external"]
else:
    statuses = ["paused_hitl", "paused_external"]

pending = CP().list_latest(statuses=statuses)

if not pending:
    st.info("مفيش طلبات معلقة حاليًا")

# دالة الاستئناف المناسبة لكل graph
DECIDE_MANAGER = {
    "purchase_order": po_decide_manager,
    "inventory_approval": inv_decide,
    "warranty_claim": warranty_decide_manager,
}
DECIDE_SUPPLIER = {
    "purchase_order": po_decide_supplier,
    "warranty_claim": warranty_decide_supplier,
}

for cp in pending:
    with st.expander(f"{cp.thread_id} — {cp.graph_name} — {cp.status} @ {cp.node_name}"):
        st.write(f"**آخر تحديث:** {cp.created_at}")
        st.json(cp.state, expanded=False)

        if cp.status == "paused_hitl" and cp.graph_name in DECIDE_MANAGER:
            fn = DECIDE_MANAGER[cp.graph_name]
            c1, c2 = st.columns(2)
            if c1.button("موافقة ✅", key=f"hitl-approve-{cp.thread_id}"):
                r = fn(cp.thread_id, True)
                st.write(f"status={r.status}")
                st.rerun()
            if c2.button("رفض ❌", key=f"hitl-reject-{cp.thread_id}"):
                r = fn(cp.thread_id, False)
                st.write(f"status={r.status}")
                st.rerun()

        if cp.status == "paused_external" and cp.graph_name in DECIDE_SUPPLIER:
            fn = DECIDE_SUPPLIER[cp.graph_name]
            note = st.text_input("ملاحظة", key=f"hitl-note-{cp.thread_id}")
            c1, c2 = st.columns(2)
            if c1.button("رد المورد: موافق ✅", key=f"hitl-ext-approve-{cp.thread_id}"):
                r = fn(cp.thread_id, "approved" if cp.graph_name == "warranty_claim" else "confirmed", note=note)
                st.write(f"status={r.status}")
                st.rerun()
            if c2.button("رد المورد: رفض ❌", key=f"hitl-ext-reject-{cp.thread_id}"):
                r = fn(cp.thread_id, "rejected", note=note)
                st.write(f"status={r.status}")
                st.rerun()