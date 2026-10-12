export type WmsContract = {
  pk: number;
  number: string;
  acceptance_date: string | null;
  warranty_months: number;
  updated_at: string;
  acceptance_followers: number;
  warranty_followers: number;
};

export type WmsReceiptDetail = {
  pk: number;
  receipt: number;
  receipt_location: number;
  part: number;
  equipment: string;
  name: string;
  project: string;
  project_id?: number | null;
  contract: number;
  contract_number: string;
  detail_uid: string;
  prepared_quantity: string;
  acceptance_date: string | null;
  warranty_months: number;
  warranty_end: string | null;
  label_version: number;
  barcode: string;
  state: 'READY' | 'IN_REQUEST' | 'RECEIVED' | 'CANCELLED';
  active_request: number | null;
  actual_quantity: string | null;
  stock?: Array<{
    pk: number;
    location: number;
    location_name: string;
    quantity: string;
    frozen: string;
    allocated: string;
    available: string;
  }>;
};

export type WmsReceipt = {
  pk: number;
  project: string;
  project_id?: number | null;
  contract: number;
  location: number;
  cancelled: boolean;
  details: WmsReceiptDetail[];
};
