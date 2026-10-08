# MySQL → PostgreSQL (Azure UAT) — Discovery Questionnaire

**Context for the reader:** 21 tables is a small data-movement job. The effort and risk concentrate in three places — (a) type semantics that differ silently between the two engines, (b) application SQL/ORM rewrite, (c) cutover and access logistics in Azure. The questions below are ordered so the ones that block all other work come first.

Each question is followed by **Why** — the concrete failure it prevents — so a reader can judge whether it applies to their situation.

---

## 1. Blocking questions — nothing can start until these are answered

**1. What is the source exactly — on-prem MySQL, Azure Database for MySQL Flexible Server, AWS RDS, or Aurora MySQL? And the exact version (`SELECT VERSION()`)?**

**Why:** The hosting model decides your network path and whether you can even enable binlog-based change capture (managed services restrict server parameters; Aurora behaves differently again). The version matters because MySQL 5.7 and 8.0 have different default collations (`utf8mb4_general_ci` vs `utf8mb4_0900_ai_ci`), different JSON function sets, and different `sql_mode` defaults — which directly determines how much invalid data is sitting in the source. MySQL 5.7 is also past end-of-life, so if that's the source, the migration may be a compliance driver rather than a nice-to-have.

**2. What is the target exactly — Azure Database for PostgreSQL Flexible Server, Cosmos DB for PostgreSQL, or PostgreSQL on an Azure VM? Which PG major version is approved?**

**Why:** These three have materially different capabilities. Flexible Server is managed but restricts superuser and extensions; Cosmos DB for PostgreSQL (Citus) requires you to choose distribution columns, which is a schema design exercise, not a lift-and-shift; a VM gives full control but transfers all patching and backup responsibility to you. The major version controls which extensions and syntax are available. Picking this late means re-doing the schema conversion.

**3. Is UAT the end goal, or a rehearsal for a production migration?**

**Why:** This is the single highest-leverage question. If UAT is a rehearsal, every step must be scripted, version-controlled, and repeatable — because you'll run it again on prod under time pressure with real users waiting. If UAT is the end goal (e.g. a test environment being modernised), manual steps are acceptable and you can move much faster. Teams routinely hand-fix UAT data, then discover at prod cutover that nothing is reproducible.

**4. One-time (offline) load, or continuous replication (CDC)? What downtime window is acceptable, and how long?**

**Why:** This selects the tool and roughly doubles or halves the project. An offline load of 21 tables can be done with `pgloader` in an afternoon. A zero-downtime online migration needs change-data-capture infrastructure (Debezium, or a commercial tool), a dual-write or replication-lag monitoring strategy, and a reconciliation process — weeks of work. Ask for the window in minutes/hours, not "minimal".

**5. Who owns the application layer — is the app's SQL being rewritten by the same team, or do we only own the database?**

**Why:** In heterogeneous migrations the database is typically 30% of the effort and the application is 70%. MySQL-specific SQL does not run on PostgreSQL: backtick quoting, `LIMIT x, y` offset syntax, `IFNULL`, `GROUP_CONCAT`, `DATE_FORMAT`, `INSERT ... ON DUPLICATE KEY UPDATE`, `REPLACE INTO`, implicit type coercion in comparisons, and non-standard `GROUP BY` all break. If nobody owns that rewrite, the migration will technically succeed and the application will still fail UAT. Establish ownership before you start, not at test time.

**6. What is the total data volume, and the row count and size of the largest table? Any large BLOB/TEXT columns?**

**Why:** Determines load duration, whether you can do a single-pass dump-and-load or need chunked/parallel loading, and the storage/IOPS tier to provision. Large binary columns also change the network and memory profile of the load tool and are a common cause of mid-load failures.

**7. Has a migration tool already been mandated or approved?**

**Why:** Procurement and security approval for new tooling is often the longest lead time in the project — longer than the migration itself. Important caveat for Azure specifically: **Azure Database Migration Service does not cover heterogeneous MySQL → PostgreSQL** (that scenario was retired; worth re-verifying against current Azure docs for your subscription, as this area changes). People frequently assume "we'll just use Azure DMS" and lose two weeks discovering otherwise. Realistic options to put forward:

| Tool | Best for | Trade-off |
|---|---|---|
| **pgloader** | One-time offline load; best default here | Open-source, no vendor support |
| **Azure Data Factory** | Orgs where ADF is already governed and in use | More plumbing; weaker type conversion |
| **Debezium / Kafka** | Genuine near-zero-downtime CDC | Significant infrastructure to stand up |
| **Striim / Qlik / Fivetran** | Fastest to stand up | Slowest to get approved; licence cost |

**8. Will the UAT PostgreSQL server be reachable from the source? Private Endpoint or public access with firewall rules? Which VNet/subnet, and who raises the NSG/firewall request?**

**Why:** Connectivity is the most common cause of a stalled day one. Azure PostgreSQL Flexible Server must be deployed as either public-access or VNet-integrated **at creation time** — you cannot freely switch afterwards, so getting this wrong means deleting and recreating the server. Naming the person who raises the firewall request matters because that ticket usually sits in someone else's queue.

---

## 2. Don't ask — profile it yourself (do this today)

**Why this section exists:** Most of what gets discussed in a requirements workshop is already written down in the source database's catalog. Asking for a read-only credential and running these queries replaces several meetings and converts vague questions into a concrete decision list with owners against it. Do this before you book anything.

```sql
-- Sizes and row counts: drives load strategy, sequencing, and SKU sizing
SELECT table_name, engine, table_rows,
       ROUND((data_length+index_length)/1024/1024,1) AS mb
FROM information_schema.tables
WHERE table_schema='<db>' ORDER BY mb DESC;

-- The types that actually cause pain (see section 3 for what each one means)
SELECT table_name, column_name, column_type, is_nullable, column_default, extra
FROM information_schema.columns
WHERE table_schema='<db>'
  AND (data_type IN ('enum','set','tinyint','year','bit','json','blob','mediumblob',
                     'longblob','datetime','timestamp','double','decimal','char')
       OR column_type LIKE '%unsigned%'
       OR extra LIKE '%auto_increment%'
       OR column_default='0000-00-00 00:00:00');

-- Objects no tool will convert for you — these are hand-ported effort
SELECT routine_type, routine_name FROM information_schema.routines WHERE routine_schema='<db>';
SELECT trigger_name, event_object_table FROM information_schema.triggers WHERE trigger_schema='<db>';
SELECT table_name FROM information_schema.views WHERE table_schema='<db>';

-- Referential integrity: load order, and whether CASCADE semantics are relied on
SELECT table_name, constraint_name, delete_rule, update_rule
FROM information_schema.referential_constraints WHERE constraint_schema='<db>';

-- Charset/collation: predicts case-sensitivity and encoding breakage
SELECT DISTINCT character_set_name, collation_name
FROM information_schema.columns WHERE table_schema='<db>';
```

**Why each query earns its place:** the first sizes the environment and sequences the load; the second produces your type-mapping sheet, which is the real deliverable of discovery; the third quantifies manual conversion effort (stored procedures and triggers do not auto-convert — MySQL's procedural dialect and PL/pgSQL are different languages); the fourth tells you the order tables must be loaded in and whether you can defer foreign keys; the fifth predicts the case-sensitivity class of bugs described below.

---

## 3. Schema and data-type decisions

**Why this whole section matters:** these are *silent* differences. The migration tool will not error — it will load data that behaves differently afterwards. Each needs a named decision, ideally recorded in the type-mapping sheet (template in the appendix).

**9. ENUM and SET columns — convert to a PostgreSQL `enum` type, `text` + `CHECK`, or a lookup table?**

**Why:** PostgreSQL has `enum`, but adding values to it is a DDL operation and ordering is fixed at creation, which annoys application teams later. `SET` has **no PostgreSQL equivalent at all** — it must become an array, a bitmask, or a child table, which is a schema redesign requiring application changes. This cannot be deferred to the tool.

**10. `tinyint(1)` columns — are they genuinely boolean, or small integers?**

**Why:** MySQL has no real boolean type; `tinyint(1)` is the convention, and most MySQL drivers silently present it as true/false. If you map it to `smallint` in PostgreSQL, application code doing `if (flag)` or passing a boolean parameter breaks; if you map it to `boolean` but some rows hold the value 2 or 7, the load fails or data is lost. You must inspect actual distinct values, not just the declared type.

**11. `UNSIGNED` integer columns — do any hold values above the signed maximum?**

**Why:** PostgreSQL has no unsigned integer types. `INT UNSIGNED` generally fits in `bigint`, but `BIGINT UNSIGNED` (max ≈1.8×10^19) **exceeds** PostgreSQL's `bigint` (max ≈9.2×10^18) and needs `numeric`, which is slower and changes index behaviour. Also, any `CHECK (col >= 0)` guarantee that MySQL gave you for free must now be written explicitly, or negative values become possible where the app never expected them.

**12. Zero dates (`0000-00-00`) and otherwise invalid dates — map to `NULL`, a sentinel date, or reject the row?**

**Why:** MySQL with non-strict `sql_mode` happily stores `0000-00-00 00:00:00`. PostgreSQL rejects it outright. Without a decision, your bulk load dies partway through on a row you didn't know existed, often hours in. Decide the mapping and apply it as a transformation, and get the business to confirm what a zero date *meant* (unknown? never? not applicable?).

**13. `datetime` vs `timestamp` — should the target be `timestamptz` or `timestamp`? What timezone is the data in now, and what does the application assume?**

**Why:** MySQL `datetime` stores no timezone; `timestamp` converts to UTC on write using the session timezone and back on read. PostgreSQL's `timestamptz` stores UTC and converts on read. Mapping a `datetime` straight to `timestamptz` makes PostgreSQL interpret existing values using the *server's* timezone, shifting every timestamp in the database by the UTC offset — a very common, very hard-to-spot data corruption that only surfaces in reports weeks later. Pin down the current convention explicitly.

**14. Case sensitivity: MySQL's default collations are case-insensitive, PostgreSQL's are case-sensitive. Will this break lookups, and do we need `citext` or functional indexes?**

**Why:** This is the number-one post-migration production incident in MySQL→PG moves. `WHERE email = 'User@x.com'` matched in MySQL and will not match in PostgreSQL. Worse, in the other direction: a `UNIQUE` index that prevented both `'bob'` and `'Bob'` in MySQL will now permit both, so duplicates appear in data the application believed was unique. Fixes are `citext`, `lower()` functional indexes, or a non-deterministic ICU collation — but all of them must be chosen deliberately per column.

**15. Identifier casing — mixed-case table/column names become lowercase in PostgreSQL. Who renames, and does the application follow?**

**Why:** PostgreSQL folds unquoted identifiers to lowercase, so `OrderItems` becomes `orderitems` unless quoted everywhere forever. Quoting everywhere is a long-term maintenance tax; renaming requires application and ORM changes. Pick one, document it, and apply it consistently — mixing the two is the worst outcome.

**16. AUTO_INCREMENT → `GENERATED BY DEFAULT AS IDENTITY` or `serial`? Who resets the sequence high-water marks after the load?**

**Why:** Bulk-loading rows with explicit ID values does **not** advance the PostgreSQL sequence. The migration looks perfect, every row reconciles, UAT starts — and the very first `INSERT` fails with a duplicate primary key. This is a near-universal first-day bug and takes one `setval()` per table to prevent, so make it an explicit, owned post-load step rather than something someone remembers.

**17. Other semantic gaps — stored procedures, triggers, `ON UPDATE CURRENT_TIMESTAMP`, full-text indexes, spatial types, generated columns, and `utf8mb4` 4-byte data.**

**Why:** Each needs manual work that no converter does for you. MySQL's `ON UPDATE CURRENT_TIMESTAMP` requires a PostgreSQL trigger function to replicate. MySQL `FULLTEXT` indexes must be rebuilt as `tsvector` + GIN indexes with explicit search-query rewrites in the app. Spatial types need PostGIS, which must be allowlisted on Azure. And if the source is MySQL's old 3-byte `utf8` or has UTF-8 bytes stored in `latin1` columns, you get mojibake (double-encoded text) on load — detectable only by eyeballing non-ASCII data, so check it deliberately.

**18. Is schema change in scope, or is this a like-for-like lift?**

**Why:** Teams are tempted to fix modelling debt during migration. Resist for UAT: if both the engine and the schema change at once, any failure has two possible causes and debugging time multiplies. Recommend like-for-like, with improvements as a tracked follow-up — and get that agreed in writing, because it's the scope question most likely to be reopened mid-project.

---

## 4. Azure target and environment

**19. Which subscription, resource group, and region — and is there an existing naming and tagging standard?**

**Why:** Tagging is usually mandatory for cost allocation and will be flagged in a compliance scan if missed. Region matters for data-residency rules (relevant under DPDP/GDPR) and for latency between the app tier and the database — a UAT database in a different region from the UAT app makes every performance number meaningless.

**20. What SKU — vCores, memory, storage tier, IOPS? Is UAT sized like production or deliberately smaller?**

**Why:** Smaller UAT is a legitimate cost decision, but it **invalidates performance sign-off**. If UAT is undersized, say so up front so nobody treats UAT query timings as a production prediction. Note also that on Azure PostgreSQL Flexible Server, IOPS are tied to the provisioned storage size — so a small disk silently caps your load throughput.

**21. What HA, backup retention, and point-in-time-restore settings does UAT need?**

**Why:** Zone-redundant HA roughly doubles cost and is rarely needed in UAT. But you *do* want working backups — because you will break UAT at least once, and restoring beats reloading. Decide consciously rather than accepting defaults.

**22. Which PostgreSQL extensions are required (`pg_stat_statements`, `uuid-ossp`, `citext`, `postgis`, …)?**

**Why:** On Azure PostgreSQL Flexible Server, extensions must be allowlisted via the `azure.extensions` server parameter before `CREATE EXTENSION` will work, and some changes require a server restart. Discovering mid-load that `citext` isn't allowlisted means a restart window you didn't plan for. Also note not every PostgreSQL extension is available on Azure at all — verify the ones you depend on early.

**23. What authentication model — native PostgreSQL roles, or Microsoft Entra ID? Who holds the admin credential, and where do secrets live?**

**Why:** Entra ID authentication changes how the application connects (token-based, with expiry and refresh handling) — that's application code, not configuration. If the app can only do username/password, you need native roles. And if the admin password lives in one engineer's notes rather than Key Vault, you have both an audit finding and a bus-factor problem.

**24. Is TLS enforced, and what's the minimum TLS version?**

**Why:** Azure PostgreSQL Flexible Server requires encrypted connections by default. Application connection strings need `sslmode=require` (or stricter, with the CA chain bundled). Clients that connected to MySQL unencrypted will fail immediately with an opaque error — worth pre-empting since it looks like a network problem rather than a TLS one.

**25. Who has provisioning rights — do we get Contributor on the resource group, or does everything go through a platform team ticket? What's the SLA?**

**Why:** This determines your realistic timeline more than any technical factor. "We need a server" is an hour with Contributor access and a week through a ticket queue. Ask for the SLA number so the project plan reflects reality.

**26. Does infrastructure-as-code exist (Terraform/Bicep), and must the UAT server be created through it?**

**Why:** If IaC is mandated, a portal-clicked server will be flagged or destroyed by the next pipeline run. If IaC exists but isn't mandated, writing it still pays off, because this UAT server will be rebuilt more than once.

---

## 5. Migration mechanics and cutover

**27. Confirm the sequence and who signs off each gate: schema → bulk data → indexes and constraints → foreign keys → sequence reset → validation.**

**Why:** Order is a performance decision, not just procedure. Loading data *before* creating indexes and foreign keys is dramatically faster, because every index maintains itself per row otherwise. Creating them first is the most common reason a load that should take 20 minutes takes 6 hours. Named gate owners prevent the load being declared "done" before sequences and constraints exist.

**28. Is binlog enabled on the source, with `binlog_format=ROW` and `binlog_row_image=FULL`? What's the retention period?**

**Why:** Any CDC-based approach is impossible without these, and on managed MySQL you may not be able to change them without a restart and a change ticket. Retention matters because if the initial snapshot takes longer than the binlog retention window, the change stream can't catch up and you have to start over.

**29. Do all 21 tables have primary keys?**

**Why:** CDC and logical replication need a way to identify a row to apply an update or delete. A table without a primary key (or unique index) either can't be replicated or needs `REPLICA IDENTITY FULL`, which is expensive. Tables without PKs also make reconciliation much harder — you can compare counts but not individual rows.

**30. Can we split reference/lookup tables from transactional tables and migrate the small ones first as a pilot?**

**Why:** A pilot on 3 small tables surfaces most of the toolchain, network, permission, encoding, and type issues in a day, at near-zero risk. It converts unknowns into a punch list before you've committed to a plan. With 21 tables, this is cheap.

**31. What's the rollback plan if UAT validation fails? Does the MySQL source stay writable in parallel?**

**Why:** Needs to be decided while calm, not during a failed cutover. For UAT it's usually simple (keep MySQL running, point the app back) — but confirming it explicitly is what makes the prod rehearsal meaningful, and it's the question auditors and change boards will ask.

**32. How many times will UAT be reloaded, and is a full refresh from a production snapshot allowed?**

**Why:** Drives whether you invest in automation. If testers will want fresh data repeatedly, a one-off manual load is a trap and you should build a repeatable pipeline immediately. It also interacts directly with the next question.

**33. How is PII handled in UAT — does the data need masking or anonymisation before landing in a non-production environment?**

**Why:** Ask this early, because it is the most common hidden schedule-killer. Copying production personal data into UAT may be prohibited under DPDP/GDPR and internal policy, and building a masking pipeline (consistent, referentially-intact, repeatable) is a project in its own right. Discovering this requirement after the data is already loaded means deleting it and starting again — plus a potential incident report.

**34. Is there a freeze window on the source during migration?**

**Why:** For an offline load, writes arriving mid-copy mean your reconciliation will never balance and you won't know whether the mismatch is a bug or just new data. Either freeze writes or switch to CDC — but know which before you start validating.

---

## 6. Validation and sign-off

**35. What exactly is the acceptance criterion — row counts, column-level checksums/aggregates, or full application regression testing?**

**Why:** Row counts prove nothing about correctness: every row can be present with timestamps shifted by 5½ hours, booleans inverted, or text mojibaked. Agree the depth of validation up front, because "migration complete" means wildly different things to a DBA and to a business owner, and that gap is where projects stall at 95% done.

**36. Who owns the UAT test cases, and do they already exist?**

**Why:** If regression tests don't exist, the migration project will be expected to invent them — which is weeks of unplanned work by people who don't know the business rules. Find out now whether you're inheriting a test pack or writing one.

**37. Do we have a current MySQL performance baseline — query timings for the key workloads?**

**Why:** Without a baseline, "it's slower on PostgreSQL" is an unfalsifiable complaint, and you'll spend the tail of the project chasing anecdotes. Capture timings on MySQL *before* cutover (and enable `pg_stat_statements` on the target) so comparison is data-driven. Expect some queries to genuinely regress — PostgreSQL's planner makes different choices and may need added indexes or rewritten queries.

**38. What reconciliation tolerance is acceptable — exactly zero drift, or is in-flight data allowed to differ?**

**Why:** Determines whether you need a freeze window (question 34) and how much reconciliation engineering is justified. Chasing a zero-drift target against a live source is a lot of effort to spend unnecessarily.

**39. Who is the single named person who approves "UAT migration complete"?**

**Why:** Migrations without a named approver don't end — they drift as each stakeholder adds a condition. One name converts an open-ended project into a finishable one.

---

## 7. Logistics and governance

**40. What's the hard deadline, and what's actually driving it (audit, licence expiry, a production cutover date, a release train)?**

**Why:** The reason behind the date tells you what can flex. A licence expiry is immovable; an internal preference is negotiable. It also reveals the real success criterion — e.g. if a MySQL support contract is expiring, "off MySQL" matters more than "optimal schema".

**41. What are the lead times for access requests — database credentials, Azure RBAC, VPN, firewall rules?**

**Why:** This is almost always the true critical path, and it's invisible in technical plans. Collect the numbers and put them in the schedule as real dependencies with real durations.

**42. Is a change ticket or CAB approval required for UAT changes?**

**Why:** Some organisations govern UAT as tightly as production. Finding that out the evening you planned to cut over is avoidable.

**43. What are the monitoring and alerting expectations, and who receives the alerts?**

**Why:** An unmonitored UAT database fills its disk or hits connection limits and gets blamed on the migration. Basic metric alerts plus a named recipient prevent that, and establish the pattern you'll need for prod.

---

## Suggested first 48 hours

**Why this plan:** it front-loads everything that can be done without waiting on other people's answers, and turns the remaining open questions into a single decision meeting rather than a series of workshops.

1. **Get read-only source credentials and the Azure resource group, then run the section-2 profiling queries.** Highest information gain per hour of the whole project.
2. **Produce the type-mapping sheet** — all 21 tables, every column, MySQL type → PostgreSQL type → decision + owner for the ambiguous ones. This artifact *is* your design document, and it makes the work reviewable by people who aren't in the room.
3. **Provision the smallest viable Azure PostgreSQL Flexible Server and prove end-to-end connectivity.** Fails early and loudly if networking or permissions are wrong — the two things most likely to cost you a week.
4. **Run `pgloader` against the 3 smallest tables as a one-day spike.** Surfaces roughly 80% of real type, encoding, and permission issues immediately, at almost no cost or risk.
5. **Take the open decisions (ENUM/SET, `tinyint(1)`, `timestamptz`, case sensitivity, PII masking) into one 45-minute decision meeting.** By then they're specific questions with evidence attached, which is the difference between a decision meeting and a discussion.

**The one thing to do before any meeting:** run the profiling queries. They convert most of section 3 from open-ended questions into a short decision list with names against it — which is what actually gets a migration moving.

---

## Appendix A — Type-mapping sheet template

One row per column that needs a decision (populate from the section-2 profiling query).

| Table | Column | MySQL type | Proposed PG type | Decision needed | Owner | Status |
|---|---|---|---|---|---|---|
| | | `tinyint(1)` | `boolean` | Confirm only 0/1 present | | Open |
| | | `enum(...)` | `text` + `CHECK` | enum vs lookup table | | Open |
| | | `datetime` | `timestamptz` | Source timezone convention | | Open |
| | | `bigint unsigned` | `numeric` | Any value > 9.2e18? | | Open |
| | | `varchar` ci | `citext` | Case-insensitive lookups? | | Open |

## Appendix B — Quick reference: common MySQL → PostgreSQL type mappings

| MySQL | PostgreSQL | Note |
|---|---|---|
| `tinyint(1)` | `boolean` | Verify distinct values first |
| `tinyint`/`smallint` | `smallint` | |
| `int unsigned` | `bigint` | No unsigned in PG |
| `bigint unsigned` | `numeric` | Exceeds PG `bigint` range |
| `datetime` | `timestamp` / `timestamptz` | Decide deliberately — see Q13 |
| `timestamp` | `timestamptz` | MySQL already stores UTC |
| `enum(...)` | `enum` / `text`+`CHECK` / FK | Design decision |
| `set(...)` | array / child table | No equivalent |
| `double` | `double precision` | |
| `decimal(p,s)` | `numeric(p,s)` | |
| `tinytext`…`longtext` | `text` | |
| `blob` variants | `bytea` | |
| `json` | `jsonb` | `jsonb` preferred; function names differ |
| `year` | `smallint` | |
| `bit(n)` | `bit(n)` / `boolean` | |
| `AUTO_INCREMENT` | `GENERATED ... AS IDENTITY` | Reset sequences post-load (Q16) |
