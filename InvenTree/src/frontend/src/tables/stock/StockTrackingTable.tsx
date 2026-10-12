import { t } from '@lingui/core/macro';
import { Group, Table, Text } from '@mantine/core';
import { type ReactNode, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';

import { ApiEndpoints } from '@lib/enums/ApiEndpoints';
import { ModelType } from '@lib/enums/ModelType';
import { apiUrl } from '@lib/functions/Api';
import { formatDecimal } from '@lib/functions/Formatting';
import useTable from '@lib/hooks/UseTable';
import type { TableFilter } from '@lib/types/Filters';
import type { TableColumn } from '@lib/types/Tables';
import { RenderBuildOrder } from '../../components/render/Build';
import { RenderCompany } from '../../components/render/Company';
import {
  RenderPurchaseOrder,
  RenderReturnOrder,
  RenderSalesOrder,
  RenderTransferOrder
} from '../../components/render/Order';
import { RenderPart } from '../../components/render/Part';
import { StatusRenderer } from '../../components/render/StatusRenderer';
import {
  RenderStockItem,
  RenderStockLocation
} from '../../components/render/Stock';
import { RenderUser } from '../../components/render/User';
import {
  DateColumn,
  DescriptionColumn,
  IPNColumn,
  PartColumn,
  StockColumn
} from '../../components/tables/ColumnRenderers';
import {
  IncludeVariantsFilter,
  MaxDateFilter,
  MinDateFilter,
  UserFilter
} from '../../components/tables/Filter';
import { InvenTreeTable } from '../../components/tables/InvenTreeTable';

type StockTrackingEntry = {
  label: string;
  key: string;
  details: ReactNode;
};

export type StockTrackingScope = 'global' | 'part' | 'item';

export function StockTrackingTable({
  itemId,
  partId,
  scope
}: Readonly<{
  itemId?: number;
  partId?: number;
  scope?: StockTrackingScope;
}>) {
  const navigate = useNavigate();
  const resolvedScope: StockTrackingScope =
    scope ?? (itemId ? 'item' : partId ? 'part' : 'global');
  const table = useTable(
    resolvedScope === 'part'
      ? 'part_stock_tracking'
      : resolvedScope === 'global'
        ? 'global_stock_tracking'
        : 'stock_tracking'
  );

  // Render "details" for a stock tracking record
  const renderDetails = useCallback(
    (record: any) => {
      const deltas: any = record?.deltas ?? {};
      const destination = deltas.location_detail
        ? RenderStockLocation({
            instance: deltas.location_detail,
            link: true,
            navigate: navigate
          })
        : null;
      const source = deltas.old_location_detail
        ? RenderStockLocation({
            instance: deltas.old_location_detail,
            link: true,
            navigate: navigate
          })
        : null;
      const locationDetails =
        record.tracking_type === 20 && deltas.location ? (
          <Group gap='xs' wrap='nowrap'>
            {source ?? <Text c='dimmed'>{t`Unknown location`}</Text>}
            <Text aria-hidden='true'>→</Text>
            {destination ?? <Text c='dimmed'>{t`Unknown location`}</Text>}
          </Group>
        ) : (
          destination
        );
      const quantityLabel = [10, 11, 12].includes(record.tracking_type)
        ? t`Stock after operation`
        : record.tracking_type === 20
          ? t`Quantity moved`
          : t`Quantity`;

      const entries: StockTrackingEntry[] = [
        {
          label: t`Stock Item`,
          key: 'stockitem',
          details:
            deltas.stockitem_detail &&
            RenderStockItem({ instance: deltas.stockitem_detail, link: true })
        },
        {
          label: t`Stock Item`,
          key: 'item',
          details:
            deltas.item_detail &&
            RenderStockItem({
              instance: deltas.item_detail,
              link: true
            })
        },
        {
          label: t`Status`,
          key: 'status',
          details:
            deltas.status &&
            StatusRenderer({
              status: deltas.status,
              type: ModelType.stockitem,
              fallbackStatus: deltas.status_logical
            })
        },
        {
          label: t`Old Status`,
          key: 'old_status',
          details:
            deltas.old_status &&
            StatusRenderer({
              status: deltas.old_status,
              type: ModelType.stockitem,
              fallbackStatus: deltas.old_status_logical
            })
        },
        {
          label: quantityLabel,
          key: 'quantity',
          details: formatDecimal(deltas.quantity)
        },
        {
          label: t`Added`,
          key: 'added',
          details: deltas.added
        },
        {
          label: t`Removed`,
          key: 'removed',
          details: deltas.removed
        },
        {
          label: t`Part`,
          key: 'part',
          details:
            deltas.part_detail &&
            RenderPart({
              instance: deltas.part_detail,
              link: true,
              navigate: navigate
            })
        },
        {
          label:
            record.tracking_type === 20
              ? t`Source and destination`
              : t`Location`,
          key: 'location',
          details: locationDetails
        },
        {
          label: t`Build Order`,
          key: 'buildorder',
          details:
            deltas.buildorder_detail &&
            RenderBuildOrder({
              instance: deltas.buildorder_detail,
              link: true,
              navigate: navigate
            })
        },
        {
          label: t`Purchase Order`,
          key: 'purchaseorder',
          details:
            deltas.purchaseorder_detail &&
            RenderPurchaseOrder({
              instance: deltas.purchaseorder_detail,
              link: true,
              navigate: navigate
            })
        },
        {
          label: t`Sales Order`,
          key: 'salesorder',
          details:
            deltas.salesorder_detail &&
            RenderSalesOrder({
              instance: deltas.salesorder_detail,
              link: true,
              navigate: navigate
            })
        },
        {
          label: t`Return Order`,
          key: 'returnorder',
          details:
            deltas.returnorder_detail &&
            RenderReturnOrder({
              instance: deltas.returnorder_detail,
              link: true,
              navigate: navigate
            })
        },
        {
          label: t`Transfer Order`,
          key: 'transferorder',
          details:
            deltas.transferorder_detail &&
            RenderTransferOrder({
              instance: deltas.transferorder_detail,
              link: true,
              navigate: navigate
            })
        },
        {
          label: t`Customer`,
          key: 'customer',
          details:
            deltas.customer_detail &&
            RenderCompany({
              instance: deltas.customer_detail,
              link: true,
              navigate: navigate
            })
        },
        {
          label: t`Scan event`,
          key: 'scan_event',
          details: record.scan_event ? `#${record.scan_event}` : null
        }
      ];

      return (
        <Table striped>
          <Table.Tbody>
            {entries.map(
              (entry) =>
                entry.details && (
                  <Table.Tr key={entry.key}>
                    <Table.Td>
                      <Text>{entry.label}</Text>
                    </Table.Td>
                    <Table.Td>{entry.details}</Table.Td>
                  </Table.Tr>
                )
            )}
          </Table.Tbody>
        </Table>
      );
    },
    [navigate]
  );

  const filters: TableFilter[] = useMemo(() => {
    return [
      MinDateFilter(),
      MaxDateFilter(),
      IncludeVariantsFilter(),
      UserFilter({
        name: 'user',
        label: t`User`,
        description: t`Filter by user`
      })
    ];
  }, []);

  const tableColumns: TableColumn[] = useMemo(() => {
    return [
      DateColumn({
        switchable: false
      }),
      PartColumn({
        title: t`Part`,
        part: 'part_detail',
        switchable: true,
        hidden: resolvedScope === 'item'
      }),
      IPNColumn({
        defaultVisible: false,
        hidden: resolvedScope === 'item'
      }),
      StockColumn({
        title: t`Stock Item`,
        accessor: 'item_detail',
        nullMessage: (
          <Text size='sm' c='red'>{t`Stock item no longer exists`}</Text>
        ),
        sortable: false,
        switchable: false,
        hidden: resolvedScope === 'item'
      }),
      DescriptionColumn({
        accessor: 'label'
      }),
      {
        accessor: 'details',
        title: t`Details`,
        switchable: false,
        render: renderDetails
      },
      {
        accessor: 'notes',
        title: t`Notes`,
        sortable: false,
        switchable: true
      },
      {
        accessor: 'user',
        title: t`Operator`,
        render: (record: any) => {
          if (record.user_detail) {
            return RenderUser({ instance: record.user_detail });
          }

          const snapshot =
            record.operator_display_name_snapshot ||
            record.operator_username_snapshot ||
            (record.operator_user_id_snapshot
              ? t`User #${record.operator_user_id_snapshot}`
              : '');

          return snapshot ? (
            <Text size='sm'>{snapshot}</Text>
          ) : (
            <Text size='sm' fs='italic'>{t`No user information`}</Text>
          );
        }
      },
      {
        accessor: 'scan_event',
        title: t`Scan event`,
        sortable: false,
        switchable: true,
        render: (record: any) =>
          record.scan_event ? `#${record.scan_event}` : '-'
      }
    ];
  }, [renderDetails, resolvedScope]);

  return (
    <InvenTreeTable
      tableState={table}
      url={apiUrl(ApiEndpoints.stock_tracking_list)}
      columns={tableColumns}
      props={{
        params: {
          item: itemId,
          part: partId,
          part_detail: resolvedScope === 'item' ? undefined : true,
          item_detail: resolvedScope === 'item' ? undefined : true,
          user_detail: true
        },
        enableDownload: true,
        tableFilters: filters,
        modelType: resolvedScope === 'item' ? undefined : ModelType.stockitem,
        modelField: 'item'
      }}
    />
  );
}
