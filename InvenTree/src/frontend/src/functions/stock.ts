export type StockAvailabilityRecord = {
  quantity?: number | string | null;
  allocated?: number | string | null;
  scan_frozen?: number | string | null;
  available_quantity?: number | string | null;
};

export type ScanStockNavigationAccess = {
  approvalEnabled: boolean;
  canViewStock: boolean;
  canChangeStock: boolean;
  scanApprovalPermissions?: readonly string[] | null;
};

/**
 * Return whether the scan-stock workspace should be present in navigation.
 *
 * Stock operators need view and change access. Approval and audit users only
 * need their dedicated workflow permission while scan approval is enabled.
 */
export function canAccessScanStockNavigation({
  approvalEnabled,
  canViewStock,
  canChangeStock,
  scanApprovalPermissions
}: ScanStockNavigationAccess): boolean {
  const canOperateStock = canViewStock && canChangeStock;
  const canReviewScanRequests =
    approvalEnabled &&
    (scanApprovalPermissions?.includes('approve') === true ||
      scanApprovalPermissions?.includes('audit') === true);

  return canOperateStock || canReviewScanRequests;
}

function finiteQuantity(value: unknown, fallback = 0): number {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }

  const quantity = Number(value);
  return Number.isFinite(quantity) ? quantity : fallback;
}

export function stockFrozenQuantity(
  stockItem: StockAvailabilityRecord | null | undefined
): number {
  return Math.max(0, finiteQuantity(stockItem?.scan_frozen));
}

export function stockAvailableQuantity(
  stockItem: StockAvailabilityRecord | null | undefined
): number {
  const serverQuantity = stockItem?.available_quantity;

  if (
    serverQuantity !== null &&
    serverQuantity !== undefined &&
    serverQuantity !== ''
  ) {
    const available = Number(serverQuantity);

    if (Number.isFinite(available)) {
      return Math.max(0, available);
    }
  }

  return Math.max(
    0,
    finiteQuantity(stockItem?.quantity) -
      finiteQuantity(stockItem?.allocated) -
      stockFrozenQuantity(stockItem)
  );
}
