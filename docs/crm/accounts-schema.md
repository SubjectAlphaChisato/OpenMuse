# CRM：账户与权限的数据表设计

状态：设计已确认（2026-10-27），实施中。本文是客户管理系统（CRM）第一期的地基：谁能登录、能看什么、能做什么。

## 已确认的决定

| 决定 | 结论 |
|---|---|
| 角色 | 三种：管理员、顾问、合伙人。一人一角色。 |
| 顾问之间的客户 | **默认互不可见**；要共享必须明确分配协作者。 |
| 合伙人账号 | **一家合伙人只有一个登录账号**（表结构允许多个，业务规则限制为一个）。 |
| 多人共用一段对话（群聊） | **第二期**做；表结构现在预留（见第五节）。 |
| 加权限 / 减权限 | 加权限必须管理员确认；减权限立即生效（沿用现有连接账号的规则）。 |
| 审计 | 谁看了谁、谁导出了什么，全部留痕，只追加不可改。 |

## 一、三条核心规则

1. **业务数据归公司，不归个人。** 客户、合伙人、产品、活动都带 `org_id`；账号停用数据不丢。
2. **可见范围靠两条链推导，不给每条记录单独存 ACL。** 顾问看「分配给我的客户」（`client_assignments`）；合伙人看「我所属的合伙人」（`users.partner_id`）。助理替谁工作，就用谁的身份查询。
3. **密码、邀请链接、登录令牌、恢复码一律只存哈希。**

```
organizations ─┬─ users ──────── sessions / mfa_credentials
               │    │
               │    ├─ (role=partner) ──→ partners
               │    │
               │    └─ client_assignments ──→ clients
               │
               ├─ invitations
               ├─ account_requests      （聊天里的确认卡）
               └─ audit_log             （只追加）
```

## 二、表结构

### 1. `organizations` 公司
| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(26) PK | |
| name | String(200) | |
| settings | JSONB | 是否强制两步验证、会话时长、敏感字段默认打码 |
| created_at / updated_at | | |

现在只有一家，从第一天带上，以后多公司部署不改表。

### 2. `users`（由现有 `owners` 演进）
| 字段 | 类型 | 说明 |
|---|---|---|
| id | String(26) PK | 保留现有 ID |
| org_id | FK organizations | |
| email | String(320) | `org_id + email` 唯一 |
| phone | String(32) 可空 | |
| name | String(100) | |
| password_hash | Text 可空 | 用企业微信/钉钉登录时为空 |
| role | Enum `admin` / `advisor` / `partner` | |
| partner_id | FK partners 可空 | `role='partner'` 时必填，否则必须为空（CHECK） |
| status | Enum `invited` / `active` / `suspended` / `deactivated` | |
| mfa_required | Boolean | 管理员默认 true |
| last_login_at | 可空 | |
| deactivated_at / deactivated_by | | |
| time_zone, assistant_name, response_style, preferences | 原有 | |

`owners` 改名 `users`；现有主人变成 `admin`。个人工作区的表（对话、任务、记忆、文件）的 `owner_id` 列名保持不动。

### 3. `partners`（最小版，模块⑤再扩）
`id, org_id, name, status(prospect/active/suspended/ended), created_at, updated_at`

### 4. `clients`（最小版，模块①再扩）
`id, org_id, name, status(active/archived), created_at, updated_at`

### 5. `client_assignments` 客户分配
| 字段 | 类型 | 说明 |
|---|---|---|
| id, org_id, client_id, user_id | | user 必须是 advisor 或 admin |
| kind | Enum `primary` / `collaborator` | |
| can_edit | Boolean | 协作者是否可改 |
| assigned_by / assigned_at | | |
| ended_at / ended_reason | | 转出、交接时填；不删行 |

约束：每个客户同时只能有一个有效 `primary`（部分唯一索引 `client_id WHERE kind='primary' AND ended_at IS NULL`）。

### 6. `invitations`
`id, org_id, email, phone, name, role, partner_id, initial_assignments(JSONB), token_hash(唯一), invited_by, expires_at(默认 7 天), accepted_at, accepted_user_id, revoked_at`

### 7. `sessions`（扩展现有表）
新增：`user_id`（原 owner_id）、`mfa_verified_at`、`ip`、`user_agent`、`last_seen_at`、`revoked_at`、`revoked_reason`。

### 8. `mfa_credentials`
`id, user_id, kind(totp/sms), secret_encrypted（信封加密）, verified_at, recovery_codes_hash(JSONB), created_at, last_used_at`

### 9. `account_requests` 账号变更确认卡（复制 `connection_requests` 的模式）
| 字段 | 说明 |
|---|---|
| id, org_id, requested_by, task_id, conversation_id | |
| action | `invite` / `change_role` / `assign_client` / `transfer_client` / `suspend` / `deactivate` / `reactivate` / `revoke_sessions` |
| target_user_id | |
| payload (JSONB) | 写入前已校验；卡片只渲染这里的内容 |
| reason | 只展示 |
| requires_admin | 加权限 true；减权限 false 并立即执行 |
| status | `pending` / `completed` / `declined` / `expired` / `superseded` / `failed` |
| decided_by, decided_at, result, expires_at(24h) | |

### 10. `audit_log`（扩展现有表，只追加）
新增：`org_id`、`actor_user_id`、`actor_role`（当时的角色快照）、`on_behalf_of_task_id`、`action`、`subject_type`、`subject_id`、`client_id`（索引：回答「谁看过张总」）、`ip`、`session_id`。
用 `opa db-roles` 给应用角色只授 INSERT/SELECT。

### 11. `login_attempts`
保留现有表。

## 三、权限矩阵（代码配置，不入库）

| 操作 | 管理员 | 顾问 | 合伙人 |
|---|:---:|:---:|:---:|
| 查看 / 编辑客户 | 全部 | 仅分配给我的 | — |
| 分配、转移客户 | ✅ | 只能发起 | — |
| 查看合伙人列表 | ✅ | 只读 | 仅自己 |
| 维护产品 | ✅ | — | 仅自己的 |
| 查看说明书 | ✅ | ✅ | ✅ |
| 邀请、停用、改角色 | ✅ | 只能发起 | — |
| 查看审计日志 | ✅ | — | — |
| 导出全部客户 | ✅ | 仅自己的客户 | — |
| 分享链接 | ✅ | 仅自己可见的内容 | 仅自己的产品 |

## 四、必须成立的规则

1. `role='partner'` ⇔ `partner_id` 非空（CHECK）。
2. 每个客户任何时刻恰好一个有效主责顾问。
3. 公司至少保留一个 active 管理员；最后一个不能停用自己。
4. 停用用户在同一事务内：改 status → 吊销全部 sessions → 名下分配必须已全部转出，否则拒绝。
5. 令牌只存哈希。
6. 每次查看完整敏感字段写一条审计。

## 五、群聊预留（第二期功能，表结构已在本期加入）

| 预留 | 现状 |
|---|---|
| `conversation_members`（conversation_id, user_id, role owner/member, added_by, joined_at, left_at；`conversation_id + user_id` 唯一） | 已建表。每段对话创建时自动写入创建者为 `owner`；老数据已补齐。 |
| `messages.author_user_id` | 已加列。用户消息填发言人，助理和系统消息为空；老的用户消息已补为主人。 |

第二期规则（届时实现）：
- 权限取**交集**：助理只看所有在场成员都有权看的数据。
- **谁发起谁确认**：确认卡只有发起人能点。
- 要给对方看某客户，先分配权限，再拉进对话；不能靠群聊绕过权限。

## 六、从现有系统迁移（不丢数据）

1. 新建 `organizations`，插入默认公司。
2. `owners` 改名 `users`，加 `org_id`、`role=admin`、`status=active` 等列，带默认值。
3. `sessions`、`audit_log` 加列；老记录新列为空。
4. 新建 `partners`、`clients`、`client_assignments`、`invitations`、`mfa_credentials`、`account_requests`。
5. 以后每张新业务表都带 `org_id`；客户相关带 `client_id`；合伙人相关带 `partner_id`。

## 七、有意做简单的地方

- 一人一角色，不做自定义权限组；真需要再加 `role_permissions`。
- 不做逐条记录 ACL；可见范围全部从分配和所属推导。
- 客户本人登录（第四个角色）本期不做；`users.role` 是枚举，加一个值即可。
