import type { UserProps } from '../../lib/types/User';

export function wmsAccess(
  user: UserProps | undefined,
  approvalEnabled: boolean
) {
  const administrator = !!(user?.is_staff || user?.is_superuser);
  const stock = user?.permissions?.stockitem ?? [];
  const approval = user?.permissions?.scanstockapprovalrequest ?? [];
  const canOperate =
    administrator || (stock.includes('view') && stock.includes('change'));
  const canApprove =
    approvalEnabled && (administrator || approval.includes('approve'));
  const restricted = !!user && approvalEnabled && !administrator;
  return {
    restricted,
    canOperate,
    canApprove,
    home:
      canApprove && !canOperate
        ? '/stock/scan/inbound/?tab=approval'
        : canOperate
          ? '/stock/scan/inbound/'
          : '/stock/location/index/stock-items'
  };
}

export function isWmsBusinessPath(path: string): boolean {
  return /^\/(part(?:\/|$)|stock(?:\/(?:scan|scan-inbound|location|item)(?:\/|$)|\/?$)|notifications(?:\/|$)|settings\/user(?:\/|$))/.test(
    path
  );
}
