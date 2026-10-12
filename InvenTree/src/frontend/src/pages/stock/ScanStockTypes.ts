export type ScanMode = 'inbound' | 'outbound';

export type ScanApprovalDirection = 'INBOUND' | 'OUTBOUND';

export type ScanApprovalStatus =
  | 'DRAFT'
  | 'PENDING_APPROVAL'
  | 'APPLYING'
  | 'COMPLETED'
  | 'REJECTED'
  | 'CANCELLED'
  | 'EXPIRED';

export type ScanApprovalOwnerSnapshot = {
  pk?: number;
  name?: string;
  type?: string;
  location_id?: number;
  location_path?: string;
};

export type ScanStockCandidate = {
  pk: number;
  batch: string;
  status: number;
  packaging: string;
  quantity: string;
  unallocated_quantity: string;
  expiry_date: string | null;
};

export type ScanStockEvent = {
  pk: number;
  client_event_id: string;
  operation: 'INBOUND' | 'OUTBOUND';
  outcome: 'PENDING' | 'SUCCESS' | 'FAILED';
  error_code: string;
  error_message: string;
  barcode: string;
  barcode_kind:
    | 'PART'
    | 'STOCK_ITEM'
    | 'LOCATION'
    | 'EXTERNAL'
    | 'RECEIPT_DETAIL';
  requested_quantity: string;
  applied_quantity: string;
  part_pk_snapshot: number | null;
  part_name_snapshot: string;
  part_ipn_snapshot: string;
  stock_item_pk_snapshot: number | null;
  batch_snapshot: string;
  source_detail: number | null;
  detail_uid_snapshot: string | null;
  label_version_snapshot: number | null;
  contract_number_snapshot: string;
  acceptance_date_snapshot: string | null;
  warranty_months_snapshot: number | null;
  warranty_end_snapshot: string | null;
  units_snapshot: string;
  purpose: string;
  remark: string;
  approver_snapshot: string;
  source_location_path_snapshot: string;
  destination_location_path_snapshot: string;
  stock_item_quantity_before: string | null;
  stock_item_quantity_after: string | null;
  part_total_before: string | null;
  part_total_after: string | null;
  operator_user_id_snapshot: number | null;
  operator_username_snapshot: string;
  operator_display_name_snapshot: string;
  device_id: string;
  session_id: string;
  replay_count: number;
  replayed: boolean;
  scanned_at: string;
  completed_at: string | null;
  tracking_entries: number[];
  approval_request: number | null;
  request_line_number: number | null;
  requested_part_setup: Record<string, unknown>;
  requested_stock_context: Record<string, unknown>;
  terminal_reason: string;
};

export type ScanStockApprovalRequest = {
  pk: number;
  reference: string;
  direction: ScanApprovalDirection;
  status: ScanApprovalStatus;
  location: number;
  location_path_snapshot: string;
  requester: number | null;
  requester_user_id_snapshot?: number | null;
  requester_username_snapshot: string;
  requester_display_name_snapshot: string;
  approval_owner?: number | null;
  approval_owner_snapshot?: ScanApprovalOwnerSnapshot | string | null;
  submitted_at: string | null;
  activity_at: string;
  expires_at: string;
  decided_by?: number | null;
  decided_at: string | null;
  decision_note: string;
  cancelled_by?: number | null;
  cancelled_at: string | null;
  cancel_note: string;
  last_approval_error: string;
  created_at: string;
  updated_at: string;
  line_count: number;
  active_line_count: number;
  total_quantity: string;
  scan_frozen: string;
  pending_inbound: string;
  can_cancel: boolean;
  can_approve: boolean;
  business_summary?: {
    project: string;
    contract: string;
    acceptance_date: string;
    warranty_months: string;
    warranty_end: string;
  };
  lines?: ScanStockEvent[];
};

export type ScanStockApprovalRequestList = {
  count: number;
  next?: string | null;
  previous?: string | null;
  results: ScanStockApprovalRequest[];
};

export type ScanApprovalActionResponse = ScanStockApprovalRequest & {
  error_code?: string;
  error_message?: string;
  conflict_lines?: number[];
};

export type ScanStockResponse = {
  success: boolean;
  terminal: boolean;
  replayed?: boolean;
  part_created?: boolean;
  message?: string;
  error_code?: string;
  error_message?: string;
  location?: number;
  candidates?: ScanStockCandidate[];
  event?: ScanStockEvent;
};

export type ScanSessionResult = {
  id: string;
  barcode: string;
  event?: ScanStockEvent;
  errorCode?: string;
  message: string;
  success: boolean;
  replayed?: boolean;
};

export type ScanStockStatus =
  | {
      state:
        | 'INCOMPLETE'
        | 'INVALID_LOCATION'
        | 'INVALID_BARCODE'
        | 'CONTEXT_MISMATCH';
      error_code?: string;
      error_message?: string;
    }
  | {
      state: 'NO_STOCK' | 'NO_AVAILABLE_STOCK' | 'AVAILABLE';
      scope: 'project' | 'all_projects';
      part_id: number;
      location_id: number;
      batch: string | null;
      units: string;
      quantity: string;
      frozen: string;
      allocated: string;
      available: string;
    };
