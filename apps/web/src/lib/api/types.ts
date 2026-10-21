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
export type Repo = Schemas["RepoOut"];
export type RepoStatus = Schemas["RepoStatus"];
export type ActiveJob = Schemas["ActiveJobOut"];
export type AvailableRepo = Schemas["AvailableRepoOut"];
export type AvailablePage = Schemas["AvailablePage"];
export type Estimate = Schemas["EstimateOut"];
export type Installation = Schemas["InstallationOut"];
export type IndexJob = Schemas["IndexJobOut"];
export type Stage = Schemas["Stage"];
export type JobStatus = Schemas["JobStatus"];
export type ConstellationPage = Schemas["ConstellationOut"];
export type ConstellationPoint = Schemas["ConstellationPoint"];
export type ConstellationDir = Schemas["ConstellationDir"];
export type TreeEntry = Schemas["TreeEntry"];
export type TreeListing = Schemas["TreeOut"];
export type FileContent = Schemas["FileOut"];
export type FileSearch = Schemas["SearchOut"];
