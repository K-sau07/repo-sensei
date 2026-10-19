/** Shorthands for the generated OpenAPI types (regenerate with `make openapi`). */
import type { components, paths } from "./schema";

export type { paths };
export type Schemas = components["schemas"];

export type Me = Schemas["MeResponse"];
export type MyOrg = Schemas["MyOrg"];
export type Org = Schemas["OrgOut"];
export type Member = Schemas["MemberOut"];
export type Invitation = Schemas["InvitationOut"];
export type InvitationCreated = Schemas["InvitationCreated"];
export type InvitationPreview = Schemas["InvitationPreviewOut"];
export type AuditPage = Schemas["AuditPage"];
export type SessionInfo = Schemas["SessionInfo"];
export type Role = Schemas["Role"];
