import { ActionButton } from '@lib/components/ActionButton';
import { AddItemButton } from '@lib/components/AddItemButton';
import { ApiEndpoints } from '@lib/enums/ApiEndpoints';
import { ModelType } from '@lib/enums/ModelType';
import { UserRoles } from '@lib/enums/Roles';
import { apiUrl } from '@lib/functions/Api';
import useTable from '@lib/hooks/UseTable';
import type { TableFilter } from '@lib/types/Filters';
import type { StockOperationProps } from '@lib/types/Forms';
import type { TableColumn } from '@lib/types/Tables';
import { t } from '@lingui/core/macro';
import { Alert, Button, Menu, Modal } from '@mantine/core';
import { IconDots, IconUpload } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  DateColumn,
  DescriptionColumn,
  IPNColumn,
  LocationColumn,
  PartColumn,
  StatusColumn,
  StockColumn
} from '../../components/tables/ColumnRenderers';
import {
  BatchFilter,
  CreatedAfterFilter,
  CreatedBeforeFilter,
  HasBatchCodeFilter,
  InStockFilter,
  IncludeVariantsFilter,
  IsSerializedFilter,
  ManufacturerFilter,
  SerialFilter,
  SerialGTEFilter,
  SerialLTEFilter,
  StatusFilterOptions,
  SupplierFilter,
  TagsFilter,
  UpdatedAfterFilter,
  UpdatedBeforeFilter
} from '../../components/tables/Filter';
import { InvenTreeTable } from '../../components/tables/InvenTreeTable';
import OrderPartsWizard from '../../components/wizards/OrderPartsWizard';
import {
  formatCurrency,
  formatDecimal,
  formatPriceRange
} from '../../defaults/formatters';
import { useStockFields } from '../../forms/StockForms';
import { InvenTreeIcon } from '../../functions/icons';
import {
  stockAvailableQuantity,
  stockFrozenQuantity
} from '../../functions/stock';
import { wmsAccess } from '../../functions/wms';
import { useCreateApiFormModal } from '../../hooks/UseForm';
import { useStockAdjustActions } from '../../hooks/UseStockAdjustActions';
import { WmsContractEditor } from '../../pages/stock/WmsContractEditor';
import { WmsImportPanel, wmsError } from '../../pages/stock/WmsInventoryPanel';
import {
  WmsNativeResource,
  WmsSubtotal,
  nativeInventoryDownload
} from '../../pages/stock/WmsNativeTools';
import { WmsProjectManager } from '../../pages/stock/WmsProjectSelect';
import { WmsReceiptWorkbench } from '../../pages/stock/WmsReceiptWorkbench';
import { WmsStockAttribution } from '../../pages/stock/WmsStockAttribution';
import { useGlobalSettingsState } from '../../states/SettingsStates';
import { useUserState } from '../../states/UserState';

/**
 * Construct a list of columns for the stock item table
 */
function stockItemTableColumns({
  showLocation,
  showPricing
}: {
  showLocation: boolean;
  showPricing: boolean;
}): TableColumn[] {
  return [
    PartColumn({
      accessor: 'part',
      part: 'part_detail',
      filter: ['active']
    }),
    IPNColumn({}),
    {
      accessor: 'part_detail.revision',
      title: t`Revision`,
      sortable: true,
      defaultVisible: false
    },
    DescriptionColumn({
      accessor: 'part_detail.description'
    }),
    StockColumn({
      accessor: '',
      title: t`Stock`,
      sortable: true,
      ordering: 'stock',
      filter: [
        'available',
        'allocated',
        'consumed',
        'installed',
        'in_stock',
        'sent_to_customer'
      ]
    }),
    {
      accessor: 'available_quantity',
      title: t`Available`,
      defaultVisible: false,
      render: (record: any) => formatDecimal(stockAvailableQuantity(record))
    },
    {
      accessor: 'scan_frozen',
      title: t`Frozen`,
      defaultVisible: false,
      render: (record: any) => formatDecimal(stockFrozenQuantity(record))
    },
    StatusColumn({ model: ModelType.stockitem }),
    {
      accessor: 'batch',
      sortable: true,
      copyable: true,
      filter: ['has_batch_code', 'batch']
    },
    LocationColumn({
      hidden: !showLocation,
      accessor: 'location_detail'
    }),
    {
      accessor: 'purchase_order',
      title: t`Purchase Order`,
      defaultVisible: false,
      render: (record: any) => {
        return record.purchase_order_reference;
      }
    },
    {
      accessor: 'SKU',
      title: t`Supplier Part`,
      sortable: true,
      defaultVisible: false,
      copyable: true
    },
    {
      accessor: 'MPN',
      title: t`Manufacturer Part`,
      sortable: true,
      defaultVisible: false,
      copyable: true
    },
    {
      accessor: 'purchase_price',
      title: t`Unit Price`,
      sortable: true,
      switchable: true,
      hidden: !showPricing,
      defaultVisible: false,
      render: (record: any) =>
        formatCurrency(record.purchase_price, {
          currency: record.purchase_price_currency
        })
    },
    {
      accessor: 'stock_value',
      title: t`Stock Value`,
      sortable: false,
      hidden: !showPricing,
      render: (record: any) => {
        const min_price =
          record.purchase_price || record.part_detail?.pricing_min;
        const max_price =
          record.purchase_price || record.part_detail?.pricing_max;
        const currency = record.purchase_price_currency || undefined;

        return formatPriceRange(min_price, max_price, {
          currency: currency,
          multiplier: record.quantity
        });
      }
    },
    {
      accessor: 'packaging',
      sortable: true,
      defaultVisible: false
    },
    DateColumn({
      title: t`Created`,
      accessor: 'creation_date',
      sortable: true,
      filter: ['created_before', 'created_after']
    }),
    DateColumn({
      title: t`Last Updated`,
      accessor: 'updated',
      filter: ['updated_before', 'updated_after']
    }),
    DateColumn({
      title: t`Expiry Date`,
      accessor: 'expiry_date',
      hidden: !useGlobalSettingsState.getState().isSet('STOCK_ENABLE_EXPIRY'),
      defaultVisible: false,
      filter: ['stale', 'expiry_before', 'expiry_after']
    }),
    DateColumn({
      accessor: 'stocktake_date',
      title: t`Stocktake Date`,
      sortable: true,
      filter: ['has_stocktake', 'stocktake_before', 'stocktake_after']
    })
  ];
}

/**
 * Construct a list of available filters for the stock item table
 */
function stockItemTableFilters({
  enableExpiry
}: {
  enableExpiry: boolean;
}): TableFilter[] {
  return [
    {
      name: 'active',
      label: t`Active`,
      description: t`Show stock for active parts`
    },
    {
      name: 'status',
      label: t`Status`,
      description: t`Filter by stock status`,
      choiceFunction: StatusFilterOptions(ModelType.stockitem)
    },
    {
      name: 'assembly',
      label: t`Assembly`,
      description: t`Show stock for assembled parts`
    },
    {
      name: 'allocated',
      label: t`Allocated`,
      description: t`Show items which have been allocated`
    },
    {
      name: 'available',
      label: t`Available`,
      description: t`Show items which are available`
    },
    {
      name: 'cascade',
      label: t`Include Sublocations`,
      description: t`Include stock in sublocations`
    },
    {
      name: 'depleted',
      label: t`Depleted`,
      description: t`Show depleted stock items`
    },
    InStockFilter(),
    {
      name: 'is_building',
      label: t`In Production`,
      description: t`Show items which are in production`
    },
    IncludeVariantsFilter(),
    SupplierFilter(),
    ManufacturerFilter(),
    {
      name: 'consumed',
      label: t`Consumed`,
      description: t`Show items which have been consumed by a build order`
    },
    {
      name: 'installed',
      label: t`Installed`,
      description: t`Show stock items which are installed in other items`
    },
    {
      name: 'sent_to_customer',
      label: t`Sent to Customer`,
      description: t`Show items which have been sent to a customer`
    },
    HasBatchCodeFilter(),
    BatchFilter(),
    IsSerializedFilter(),
    SerialFilter(),
    SerialLTEFilter(),
    SerialGTEFilter(),
    {
      name: 'tracked',
      label: t`Tracked`,
      description: t`Show tracked items`
    },
    {
      name: 'has_purchase_price',
      label: t`Has Purchase Price`,
      description: t`Show items which have a purchase price`
    },
    {
      name: 'expired',
      label: t`Expired`,
      description: t`Show items which have expired`,
      active: enableExpiry
    },
    {
      name: 'stale',
      label: t`Stale`,
      description: t`Show items which are stale`,
      active: enableExpiry
    },
    {
      name: 'expiry_before',
      label: t`Expired Before`,
      description: t`Show items which expired before this date`,
      type: 'date',
      active: enableExpiry
    },
    {
      name: 'expiry_after',
      label: t`Expired After`,
      description: t`Show items which expired after this date`,
      type: 'date',
      active: enableExpiry
    },
    UpdatedBeforeFilter(),
    UpdatedAfterFilter(),
    CreatedBeforeFilter(),
    CreatedAfterFilter(),
    {
      name: 'stocktake_before',
      label: t`Stocktake Before`,
      description: t`Show items counted before this date`,
      type: 'date'
    },
    {
      name: 'stocktake_after',
      label: t`Stocktake After`,
      description: t`Show items counted after this date`,
      type: 'date'
    },
    {
      name: 'has_stocktake',
      label: t`Has Stocktake Date`,
      description: t`Show items which have a stocktake date`
    },
    {
      name: 'external',
      label: t`External Location`,
      description: t`Show items in an external location`
    },
    TagsFilter({ modelType: ModelType.stockitem })
  ];
}

/*
 * Load a table of stock items
 */
export function StockItemTable({
  params = {},
  allowAdd = false,
  showLocation = true,
  showPricing = true,
  allowReturn = false,
  initialFilters,
  defaultInStock = true,
  tableName = 'stockitems',
  nativeInventory = false
}: Readonly<{
  nativeInventory?: boolean;
  params?: any;
  allowAdd?: boolean;
  showLocation?: boolean;
  showPricing?: boolean;
  allowReturn?: boolean;
  defaultInStock?: boolean | null;
  initialFilters?: TableFilter[];
  tableName: string;
}>) {
  const initialStockFilters: TableFilter[] = useMemo(() => {
    if (!!initialFilters) {
      return initialFilters;
    }

    const filters: TableFilter[] = [];

    // Optionally set the default "in_stock" filter
    // Typically, we default to only displaying "in_stock" items,
    // but this can be overridden by the caller if required
    if (
      !nativeInventory &&
      defaultInStock != undefined &&
      defaultInStock != null
    ) {
      filters.push({
        name: 'in_stock',
        value: defaultInStock ? 'true' : 'false'
      });
    }

    return filters;
  }, [defaultInStock, initialFilters, nativeInventory]);

  const table = useTable(tableName, {
    initialFilters: initialStockFilters
  });

  const [statistics, setStatistics] = useState<any>();
  const [scope, setScope] = useState<Record<string, any>>({});
  const navigation = useLocation();
  const [tool, setTool] = useState<string | null>(
    nativeInventory && navigation.state?.wmsTool === 'records'
      ? 'records'
      : null
  );
  const [error, setError] = useState('');
  const [contractManagementOpen, setContractManagementOpen] = useState(false);
  const user = useUserState();

  const settings = useGlobalSettingsState();
  const restricted = wmsAccess(
    user.user,
    settings.isSet('SCAN_APPROVAL_ENABLED')
  ).restricted;

  const stockExpiryEnabled = useMemo(
    () => settings.isSet('STOCK_ENABLE_EXPIRY'),
    [settings]
  );

  const tableColumns = useMemo(() => {
    const columns = stockItemTableColumns({ showLocation, showPricing });
    if (!nativeInventory) return columns;
    return [
      ...[
        ['contract_number', t`合同号`],
        ['detail_uid', t`明细标识`],
        ['acceptance_date', t`验收日期`],
        ['warranty_months', t`质保月数`],
        ['warranty_end', t`质保到期日（当天仍在保）`]
      ].map(([key, title]) => ({
        accessor: `wms_warranty.${key}`,
        title,
        defaultVisible: key !== 'detail_uid',
        render: (record: any) => {
          const warranty = record.wms_warranty;
          if (key === 'detail_uid') return warranty?.[key] || '—';
          if (!warranty?.source_detail) return t`未填写`;
          return warranty[key] ?? t`待验收`;
        }
      })),
      ...columns.map((column) => ({
        ...column,
        defaultVisible: [
          'part',
          'part_detail.IPN',
          'stock',
          'batch',
          'location_detail'
        ].includes(column.accessor),
        ...(column.accessor === 'part_detail.IPN' ? { title: t`设备ID` } : {}),
        ...(column.accessor === 'batch' ? { title: t`项目` } : {}),
        ...(column.accessor === 'location_detail'
          ? { render: (row: any) => row.location_detail?.pathstring ?? '' }
          : {})
      })),
      ...[
        ['units', t`单位`],
        ['brand', t`品牌`],
        ['category', t`分类`],
        ['model_number', t`型号`],
        ['specification', t`规格`],
        ['notes', t`设备备注`]
      ].map(([key, title]) => ({
        accessor: `wms_equipment.${key}`,
        title,
        defaultVisible: key === 'units'
      })),
      { accessor: 'notes', title: t`库存备注`, defaultVisible: false },
      DateColumn({
        accessor: 'inventory_changed_at',
        title: t`最后库存变动时间`,
        defaultVisible: false
      })
    ];
  }, [showLocation, showPricing, nativeInventory]);
  const tableFilters: TableFilter[] = useMemo(
    () => [
      ...(nativeInventory
        ? [
            {
              name: 'contract',
              label: t`合同号`,
              type: 'text' as const
            },
            {
              name: 'batch',
              label: t`项目（精确匹配）`,
              type: 'text' as const
            },
            { name: 'brand', label: t`品牌`, type: 'text' as const },
            { name: 'model_number', label: t`型号`, type: 'text' as const },
            {
              name: 'category',
              label: t`分类（含下级）`,
              type: 'api' as const,
              apiUrl: apiUrl(ApiEndpoints.category_list),
              model: ModelType.partcategory
            },
            {
              name: 'cascade',
              label: t`含下级地点`,
              description: t`关闭后仅本地点`
            },
            { name: 'include_zero', label: t`包含零库存` },
            { name: 'has_stock', label: t`有无库存` },
            { name: 'has_frozen', label: t`有无冻结` }
          ]
        : []),
      ...stockItemTableFilters({ enableExpiry: stockExpiryEnabled }).filter(
        (f) => !nativeInventory || !['batch', 'cascade'].includes(f.name)
      )
    ],
    [stockExpiryEnabled, nativeInventory]
  );

  const stockOperationProps: StockOperationProps = useMemo(() => {
    return {
      items: table.selectedRecords,
      refresh: () => {
        table.clearSelectedRecords();
        table.refreshTable();
      },
      filters: {
        in_stock: true
      }
    };
  }, [table.selectedRecords, table.refreshTable]);

  const newStockItemFields = useStockFields({
    create: true,
    partId: params.part,
    supplierPartId: params.supplier_part,
    pricing: params.pricing,
    modalId: 'add-stock-item'
  });

  const newStockItem = useCreateApiFormModal({
    url: ApiEndpoints.stock_item_list,
    title: t`Add Stock Item`,
    modalId: 'add-stock-item',
    fields: newStockItemFields,
    initialData: {
      part: params.part,
      location: params.location
    },
    follow: params.openNewStockItem ?? true,
    table: table,
    modelType: ModelType.stockitem,
    successMessage: t`Stock item created`,
    keepOpenOption: true
  });

  const [attributionStock, setAttributionStock] = useState<any>(null);
  const [partsToOrder, setPartsToOrder] = useState<any[]>([]);

  const orderPartsWizard = OrderPartsWizard({
    parts: partsToOrder
  });

  const stockAdjustActions = useStockAdjustActions({
    formProps: stockOperationProps,
    return: allowReturn,
    changeBatch: true
  });

  const tableActions = useMemo(() => {
    return [
      stockAdjustActions.dropdown,
      nativeInventory && user.hasChangePermission(ModelType.stockitem) ? (
        <Button
          key='wms-contract'
          variant='light'
          disabled={table.selectedRecords.length !== 1}
          onClick={() => setAttributionStock(table.selectedRecords[0])}
        >{t`合同与验收`}</Button>
      ) : null,
      <ActionButton
        key='stock-order'
        hidden={!user.hasAddRole(UserRoles.purchase_order)}
        tooltip={t`Order items`}
        icon={<InvenTreeIcon icon='buy' />}
        disabled={!table.hasSelectedRecords}
        onClick={() => {
          setPartsToOrder(
            table.selectedRecords.map((record) => record.part_detail)
          );
          orderPartsWizard.openWizard();
        }}
      />,
      nativeInventory ? (
        allowAdd &&
        user.hasViewPermission(ModelType.stockitem) &&
        user.hasChangePermission(ModelType.stockitem) ? (
          <Button
            key='add-stock-item'
            variant='light'
            onClick={() => setTool('inbound')}
          >
            {t`入库填单与打印`}
          </Button>
        ) : null
      ) : (
        <AddItemButton
          key='add-stock-item'
          hidden={
            !allowAdd ||
            (restricted && !nativeInventory) ||
            (nativeInventory
              ? !user.hasViewPermission(ModelType.stockitem) ||
                !user.hasChangePermission(ModelType.stockitem)
              : !user.hasAddRole(UserRoles.stock))
          }
          tooltip={t`Add Stock Item`}
          onClick={() => newStockItem.open()}
        />
      )
    ];
  }, [
    user,
    allowAdd,
    nativeInventory,
    table.hasSelectedRecords,
    table.selectedRecords,
    stockAdjustActions.dropdown
  ]);

  return (
    <>
      <Modal
        opened={!!attributionStock}
        onClose={() => setAttributionStock(null)}
        title={t`合同与验收`}
      >
        {attributionStock && (
          <WmsStockAttribution
            key={attributionStock.pk}
            stock={attributionStock}
            onChanged={() => table.refreshTable()}
          />
        )}
      </Modal>
      {nativeInventory && (
        <>
          <WmsContractEditor
            opened={contractManagementOpen}
            contract={null}
            onClose={() => setContractManagementOpen(false)}
            onSaved={() => table.refreshTable()}
          />
          {error && <Alert color='red'>{error}</Alert>}
          <WmsSubtotal data={statistics} />
          <Modal
            opened={tool !== null}
            onClose={() => {
              setTool(null);
              table.refreshTable();
            }}
            title={
              tool === 'inbound'
                ? t`入库填单与打印`
                : tool === 'projects'
                  ? t`项目管理`
                  : tool === 'inventory'
                    ? t`按设备汇总`
                    : tool === 'records'
                      ? t`期间入出库记录`
                      : tool === 'parts'
                        ? t`设备资料`
                        : tool === 'import-parts'
                          ? t`设备资料导入`
                          : t`库存盘点导入`
            }
            size='90%'
            keepMounted={false}
          >
            {tool === 'inbound' ? (
              <WmsReceiptWorkbench />
            ) : tool === 'projects' ? (
              <WmsProjectManager />
            ) : tool?.startsWith('import-') ? (
              <WmsImportPanel
                key={tool}
                initialKind={tool === 'import-parts' ? 'parts' : 'count'}
                onCommitted={() => table.refreshTable()}
              />
            ) : (
              tool && (
                <WmsNativeResource
                  key={tool}
                  resource={tool as 'inventory' | 'parts' | 'records'}
                  scope={scope}
                />
              )
            )}
          </Modal>
        </>
      )}
      {newStockItem.modal}
      {orderPartsWizard.wizard}
      {stockAdjustActions.modals.map((modal) => modal.modal)}
      <InvenTreeTable
        url={apiUrl(ApiEndpoints.stock_item_list)}
        tableState={table}
        columns={tableColumns}
        props={{
          enableDownload: true,
          wrapToolbar: nativeInventory,
          onDataLoaded: nativeInventory
            ? (data, queryParams) => {
                setStatistics(data.statistics);
                const { limit, offset, ...filters } = queryParams;
                setScope(filters);
              }
            : undefined,
          onDownload: nativeInventory
            ? (filters) => {
                void nativeInventoryDownload(filters).catch((e) =>
                  setError(wmsError(e))
                );
              }
            : undefined,
          afterDownloadAction:
            nativeInventory && !restricted ? (
              <Menu position='bottom-end'>
                <Menu.Target>
                  <Button
                    size='compact-xs'
                    variant='subtle'
                    leftSection={<IconUpload size={16} />}
                    aria-label={t`导入`}
                  >{t`导入`}</Button>
                </Menu.Target>
                <Menu.Dropdown>
                  <Menu.Item
                    onClick={() => setTool('import-parts')}
                    disabled={
                      !user.hasAddPermission(ModelType.part) &&
                      !user.hasChangePermission(ModelType.part)
                    }
                  >{t`设备资料导入`}</Menu.Item>
                  <Menu.Item
                    onClick={() => setTool('import-count')}
                    disabled={!user.hasChangePermission(ModelType.stockitem)}
                  >{t`库存盘点导入`}</Menu.Item>
                </Menu.Dropdown>
              </Menu>
            ) : undefined,
          enableSelection: true,
          enableLabels: true,
          enableReports: true,
          tableFilters: tableFilters,
          tableActions: [
            ...tableActions,
            ...(nativeInventory
              ? [
                  <Menu key='wms-tools' position='bottom-end'>
                    <Menu.Target>
                      <Button
                        size='compact-xs'
                        variant='subtle'
                        aria-label={t`库存工具`}
                      >
                        <IconDots size={16} />
                      </Button>
                    </Menu.Target>
                    <Menu.Dropdown>
                      {user.hasChangePermission(ModelType.stockitem) && (
                        <Menu.Item
                          onClick={() => setTool('projects')}
                        >{t`项目管理`}</Menu.Item>
                      )}
                      {user.hasChangePermission(ModelType.stockitem) && (
                        <Menu.Item
                          onClick={() => setContractManagementOpen(true)}
                        >
                          {t`合同管理`}
                        </Menu.Item>
                      )}
                      <Menu.Item
                        onClick={() => setTool('inventory')}
                      >{t`按设备汇总`}</Menu.Item>
                      <Menu.Item
                        onClick={() => setTool('records')}
                      >{t`期间入出库记录`}</Menu.Item>
                      <Menu.Item
                        onClick={() => setTool('parts')}
                      >{t`设备资料`}</Menu.Item>
                    </Menu.Dropdown>
                  </Menu>
                ]
              : [])
          ],
          modelType: ModelType.stockitem,
          params: {
            ...params,
            ...(nativeInventory ? { wms: 'true' } : {}),
            part_detail: true,
            location_detail: true,
            supplier_part_detail: true
          }
        }}
      />
    </>
  );
}
