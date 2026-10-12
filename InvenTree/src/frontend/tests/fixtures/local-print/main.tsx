import { i18n } from '@lingui/core';
import { I18nProvider } from '@lingui/react';
import { MantineProvider } from '@mantine/core';
import '@mantine/core/styles.css';
import { QueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { createRoot } from 'react-dom/client';
import { LocalPrintModal } from '../../../src/components/printing/LocalPrintModal';
import { ApiProvider } from '../../../src/contexts/ApiContext';
import { useUserState } from '../../../src/states/UserState';

i18n.load('zh-CN', {});
i18n.activate('zh-CN');
useUserState.setState({ is_authed: true, user: { pk: 1 } as any });
const api = axios.create();
const client = new QueryClient({
  defaultOptions: { queries: { retry: false } }
});
createRoot(document.getElementById('root')!).render(
  <I18nProvider i18n={i18n}>
    <MantineProvider>
      <ApiProvider api={api} client={client}>
        <LocalPrintModal
          items={[1, 2, 3]}
          itemDetails={
            new URLSearchParams(window.location.search).has('perItem')
              ? [1, 2, 3].map((id) => ({ id, label: `设备0${id}`, copies: 1 }))
              : undefined
          }
          modelType='part'
          config={{
            enabled: true,
            qz_version: '2.2.5',
            download_url: '',
            max_labels: 500
          }}
          onClose={() => undefined}
        />
      </ApiProvider>
    </MantineProvider>
  </I18nProvider>
);
