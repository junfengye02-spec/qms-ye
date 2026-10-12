import { i18n } from '@lingui/core';
import { I18nProvider } from '@lingui/react';
import { Button, MantineProvider, Stack } from '@mantine/core';
import '@mantine/core/styles.css';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router-dom';
import { api, queryClient } from '../../../src/App';
import { ApiProvider } from '../../../src/contexts/ApiContext';
import { WmsImportPanel } from '../../../src/pages/stock/WmsInventoryPanel';
import { WmsProjectSelect } from '../../../src/pages/stock/WmsProjectSelect';
import { WmsReceiptWorkbench } from '../../../src/pages/stock/WmsReceiptWorkbench';
import { WmsStockAttribution } from '../../../src/pages/stock/WmsStockAttribution';
import { useGlobalSettingsState } from '../../../src/states/SettingsStates';
import { useUserState } from '../../../src/states/UserState';

i18n.load('zh-CN', {});
i18n.activate('zh-CN');
const { default: ScanStockPage } = await import(
  '../../../src/pages/stock/ScanStockPage'
);
queryClient.setDefaultOptions({ queries: { retry: false } });
useGlobalSettingsState.setState({
  lookup: { SCAN_APPROVAL_ENABLED: 'true' },
  loaded: true
});
useUserState.setState({
  is_authed: true,
  user: {
    pk: 1,
    is_staff: true,
    permissions: { stockitem: ['view', 'change'] }
  } as any
});
function Fixture() {
  const navigate = useNavigate();
  return (
    <Stack p='md'>
      <Button onClick={() => navigate('/stock/scan/outbound/')}>
        切换测试出入库
      </Button>
      <Button onClick={() => navigate('/inventory')}>切换测试库存维护</Button>
      <Button onClick={() => navigate('/prepare')}>切换测试填单打印</Button>
      <Button onClick={() => navigate('/imports')}>切换测试导入</Button>
      <Routes>
        <Route path='/stock/scan/:mode/' element={<ScanStockPage />} />
        <Route path='/prepare' element={<WmsReceiptWorkbench />} />
        <Route
          path='/imports'
          element={
            <>
              <WmsProjectSelect
                label='导入后项目档案'
                value=''
                required={false}
                onChange={() => {}}
              />
              <WmsImportPanel initialKind='count' />
            </>
          }
        />
        <Route
          path='/inventory'
          element={
            <WmsStockAttribution
              stock={{
                pk: 1,
                source_detail: 1,
                quantity: '5',
                part_detail: { name: '设备一' }
              }}
              onChanged={() => {}}
            />
          }
        />
      </Routes>
    </Stack>
  );
}
createRoot(document.getElementById('root')!).render(
  <I18nProvider i18n={i18n}>
    <MantineProvider>
      <MemoryRouter initialEntries={['/stock/scan/inbound/']}>
        <ApiProvider api={api} client={queryClient}>
          <Fixture />
        </ApiProvider>
      </MemoryRouter>
    </MantineProvider>
  </I18nProvider>
);
