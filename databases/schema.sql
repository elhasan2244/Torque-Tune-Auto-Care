CREATE TABLE Users (
    id INT IDENTITY(1,1) PRIMARY KEY,
    name NVARCHAR(100) NOT NULL,
    email NVARCHAR(100) NOT NULL UNIQUE,
    role NVARCHAR(20) NOT NULL CHECK (role IN ('technician', 'manager')),
    created_at DATETIME NOT NULL DEFAULT GETDATE()
);
CREATE TABLE Categories (
    id INT IDENTITY(1,1) PRIMARY KEY,
    name NVARCHAR(50) NOT NULL UNIQUE,
    description NVARCHAR(200) NULL
);
CREATE TABLE Suppliers (
    id INT IDENTITY(1,1) PRIMARY KEY,
    name NVARCHAR(100) NOT NULL,
    phone NVARCHAR(20) NULL,
    email NVARCHAR(100) NULL,
    address NVARCHAR(200) NULL
);
CREATE TABLE SpareParts (
    id INT IDENTITY(1,1) PRIMARY KEY,
    part_name NVARCHAR(100) NOT NULL,
    part_number NVARCHAR(50) NOT NULL UNIQUE,
    category_id INT NOT NULL,
    supplier_id INT NOT NULL,
    quantity INT NOT NULL CHECK (quantity >= 0),
    price DECIMAL(10,2) NOT NULL CHECK (price >= 0),
    location NVARCHAR(50) NOT NULL,
    minimum_stock INT NOT NULL DEFAULT 5 CHECK (minimum_stock >= 0),
    status NVARCHAR(20) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'discontinued')),
    FOREIGN KEY (category_id) REFERENCES Categories(id),
    FOREIGN KEY (supplier_id) REFERENCES Suppliers(id)
);
CREATE TABLE AlternativeParts (
    id INT IDENTITY(1,1) PRIMARY KEY,
    part_id INT NOT NULL,
    alternative_part_id INT NOT NULL,
    FOREIGN KEY (part_id) REFERENCES SpareParts(id),
    FOREIGN KEY (alternative_part_id) REFERENCES SpareParts(id)
);
CREATE TABLE InventoryLogs (
    id INT IDENTITY(1,1) PRIMARY KEY,
    part_id INT NOT NULL,
    user_id INT NOT NULL,
    old_quantity INT NOT NULL,
    new_quantity INT NOT NULL,
    action NVARCHAR(20) NOT NULL CHECK (action IN ('increase', 'decrease', 'set')),
    reason NVARCHAR(300) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT GETDATE(),
    FOREIGN KEY (part_id) REFERENCES SpareParts(id),
    FOREIGN KEY (user_id) REFERENCES Users(id)
);
-- ---------------------------------------------------------
-- Warranty Claims (Final Project -- state_graph/graphs/warranty_graph.py)
-- ---------------------------------------------------------
-- One row per claim submitted to a part's supplier. `status` tracks the
-- graph's own state machine (not just success/fail): a claim spends real,
-- possibly multi-day time in 'awaiting_supplier', can be rejected and then
-- appealed once (`appeal_argument` + 'appealed' -> 'awaiting_supplier'
-- again), and only ever reaches a terminal state through that loop -- this
-- is why the claim is a state graph and not a single request/response call.
CREATE TABLE WarrantyClaims (
    id INT IDENTITY(1,1) PRIMARY KEY,
    part_id INT NOT NULL,
    user_id INT NOT NULL,
    inventory_log_id INT NOT NULL,   -- which receipt this claim's warranty window is computed from
    claim_code NVARCHAR(50) NOT NULL UNIQUE,
    status NVARCHAR(30) NOT NULL DEFAULT 'submitted' CHECK (status IN (
        'submitted', 'awaiting_supplier', 'approved', 'rejected',
        'appealed', 'appeal_approved', 'appeal_rejected', 'cancelled'
    )),
    policy_check NVARCHAR(MAX) NULL,       -- RAG grounding result that established eligibility
    appeal_argument NVARCHAR(MAX) NULL,    -- Tree-of-Thoughts-selected appeal argument, if any
    supplier_response NVARCHAR(MAX) NULL,  -- raw reply captured when the external wait resolves
    created_at DATETIME NOT NULL DEFAULT GETDATE(),
    resolved_at DATETIME NULL,
    FOREIGN KEY (part_id) REFERENCES SpareParts(id),
    FOREIGN KEY (user_id) REFERENCES Users(id),
    FOREIGN KEY (inventory_log_id) REFERENCES InventoryLogs(id)
);
-- ---------------------------------------------------------
-- Purchase Orders (Final Project -- state_graph/graphs/purchase_order_graph.py)
-- ---------------------------------------------------------
-- One row per REORDER BATCH: all currently-low-stock parts from a single
-- supplier, grouped into one purchase order by that graph's Task
-- Decomposition node. `status` tracks the graph's own state machine --
-- a PO spends real, possibly multi-day time in 'awaiting_supplier'
-- (confirming price/lead time before it becomes a firm commitment), and
-- can require a manager's sign-off ('pending_approval') before it is
-- ever sent, since a confirmed PO commits real cash before the parts
-- arrive.
CREATE TABLE PurchaseOrders (
    id INT IDENTITY(1,1) PRIMARY KEY,
    supplier_id INT NOT NULL,
    requested_by_user_id INT NOT NULL,
    po_code NVARCHAR(50) NOT NULL UNIQUE,
    line_items NVARCHAR(MAX) NOT NULL,   -- JSON: [{part_id, part_number, quantity, unit_price}]
    total_cost DECIMAL(10,2) NOT NULL,
    status NVARCHAR(30) NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'pending_approval', 'awaiting_supplier',
        'confirmed', 'rejected', 'cancelled'
    )),
    supplier_response NVARCHAR(MAX) NULL,
    created_at DATETIME NOT NULL DEFAULT GETDATE(),
    resolved_at DATETIME NULL,
    FOREIGN KEY (supplier_id) REFERENCES Suppliers(id),
    FOREIGN KEY (requested_by_user_id) REFERENCES Users(id)
);
-- ---------------------------------------------------------
-- AI Agent Memory Tables (Added for Long-Term Persistence)
-- ---------------------------------------------------------
-- Stores significant events and actions (Promoted from Short-Term Memory)
CREATE TABLE EpisodicMemory (
    id INT IDENTITY(1,1) PRIMARY KEY,
    event_type NVARCHAR(100) NOT NULL,
    content NVARCHAR(MAX) NOT NULL, -- Stores the JSON representation of the event/action
    promotion_reason NVARCHAR(500) NOT NULL,
    -- 0 until a separate, periodic consolidation pass (memory/run_consolidation.py)
    -- has read this episode and tried to extract semantic facts from it. Never set
    -- by the promote-or-drop router -- that router only ever writes episodes.
    consolidated BIT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT GETDATE()
);
-- Stores consolidated long-term knowledge and user facts with version control
CREATE TABLE SemanticMemory (
    id INT IDENTITY(1,1) PRIMARY KEY,
    fact_key NVARCHAR(100) NOT NULL,
    fact_value NVARCHAR(MAX) NOT NULL, -- Stores the value (e.g., preferred setting)
    version INT NOT NULL DEFAULT 1,
    is_active BIT NOT NULL DEFAULT 1, -- 1 (True) for current active facts, 0 (False) for history
    -- Why this version was written: a fresh fact, an update, or a resolved
    -- conflict between two episodes that implied different values. Never blank
    -- on an update -- see SemanticMemory.update_fact().
    change_reason NVARCHAR(500) NULL,
    -- Optional staleness horizon (e.g. a quoted warranty/labor rate that should
    -- not be trusted forever). NULL = no expiration. Swept by
    -- SemanticMemory.expire_stale_facts(), run at the start of each
    -- consolidation pass.
    expires_at DATETIME NULL,
    updated_at DATETIME NOT NULL DEFAULT GETDATE()
);
