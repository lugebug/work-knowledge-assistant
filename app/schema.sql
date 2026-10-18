CREATE TABLE sources(id TEXT PRIMARY KEY,platform TEXT,path TEXT,sha256 TEXT,collected_at TEXT,scope TEXT);

CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,stage TEXT,boundary TEXT,next_action TEXT);

CREATE TABLE messages(id TEXT PRIMARY KEY,platform TEXT,conversation_id TEXT,conversation_name TEXT,sender_id TEXT,sender_name TEXT,event_at TEXT,message_type TEXT,body TEXT,body_hash TEXT,url TEXT,source_id TEXT REFERENCES sources(id),locator TEXT,observed_at TEXT);

CREATE TABLE documents(id TEXT PRIMARY KEY,platform TEXT,title TEXT,revision TEXT,body TEXT,url TEXT,source_id TEXT REFERENCES sources(id),kind TEXT);

CREATE TABLE evidence(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id),message_id TEXT REFERENCES messages(id),document_id TEXT REFERENCES documents(id),quote TEXT,locator TEXT,source_time TEXT,strength TEXT);

CREATE TABLE facts(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id),kind TEXT,statement TEXT,evidence_ids TEXT,interpretation TEXT);

CREATE TABLE contact_candidates(id TEXT PRIMARY KEY,platform TEXT,account_id TEXT,name TEXT,project_ids TEXT,reason TEXT,evidence_ids TEXT,identity_status TEXT);

CREATE TABLE products(id TEXT PRIMARY KEY,name TEXT,positioning TEXT,baseline_version TEXT,document_id TEXT REFERENCES documents(id));

CREATE TABLE product_capabilities(id TEXT PRIMARY KEY,product_id TEXT REFERENCES products(id),kind TEXT,name TEXT,description TEXT,verification_state TEXT,document_id TEXT REFERENCES documents(id),source_line INTEGER);

CREATE TABLE product_roadmap(id TEXT PRIMARY KEY,product_id TEXT REFERENCES products(id),name TEXT,description TEXT,status TEXT,priority TEXT,target_date TEXT,owner TEXT,document_id TEXT REFERENCES documents(id),source_line INTEGER);

CREATE TABLE product_demands(id TEXT PRIMARY KEY,product_id TEXT REFERENCES products(id),project_id TEXT REFERENCES projects(id),capability_id TEXT REFERENCES product_capabilities(id),title TEXT,stage TEXT,boundary TEXT);
