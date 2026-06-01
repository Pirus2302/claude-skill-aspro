# Эталонный TypeScript-клиент Aspro

Боевой паттерн HTTP-клиента к Aspro API на TypeScript: axios +
interceptor для exponential-backoff retry на 429/503, пагинация по 100
без доверия к `total`.

Это **референс**, не библиотека. Скопируй в свой проект, адаптируй.

## Зависимости

- Node.js 18+
- `axios`

## Конфигурация

```typescript
// config.ts
export const config = {
  aspro: {
    host: process.env.ASPRO_HOST!,         // "mycompany.aspro.cloud"
    apiKey: process.env.ASPRO_API_KEY!,
  },
};
```

## Клиент с retry-interceptor

```typescript
import axios, { AxiosError } from 'axios';
import { config } from './config.js';

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2000;

const baseUrl = () => `https://${config.aspro.host}`;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const api = () => {
  const b = baseUrl();
  if (!b || !config.aspro.apiKey) throw new Error('Aspro not configured');
  const client = axios.create({
    baseURL: b,
    params: { api_key: config.aspro.apiKey },
    timeout: 60000,
  });
  // Retry на 429/503 — exponential backoff.
  // Внимание: НЕ повторяем на 4xx кроме 429 — это наша ошибка.
  client.interceptors.response.use(undefined, async (err: AxiosError) => {
    const status = err.response?.status;
    const retryCount = (err.config as { _retryCount?: number })._retryCount ?? 0;
    if ((status === 429 || status === 503) && retryCount < MAX_RETRIES) {
      const delay = RETRY_DELAY_MS * Math.pow(2, retryCount);
      (err.config as { _retryCount?: number })._retryCount = retryCount + 1;
      await sleep(delay);
      return client.request(err.config!);
    }
    throw err;
  });
  return client;
};
```

## Типы основных сущностей

```typescript
export type AsproProject = {
  id: number;
  name: string;
  manager_id?: number;
  is_archive?: number;
  startdate?: string;
  enddate?: string;
  updated_at?: string;
};

export type AsproStage = {
  id: number;
  name: string;
  project_id: number;
  checked?: number;
  updated_date?: string;
};

export type AsproTask = {
  id: number;
  name: string;
  model_id: number;
  project_stage_id?: number;
  responsible_id?: number;
  status: number;
  deadline?: string;
  priority?: number;
  time_estimate?: number;
  closed_date?: string;
  updated_at?: string;
};
```

## Пагинация по 100, без доверия к `total`

```typescript
function paginate<T>(
  url: string,
  filter?: Record<string, string | number>,
): Promise<T[]> {
  const client = api();
  const all: T[] = [];
  let page = 1;
  const limit = 100;

  const fetchPage = async (): Promise<T[]> => {
    const res = await client.get(url, { params: { page, limit, ...filter } });
    const items = (res.data?.response?.items ?? []) as T[];
    all.push(...items);
    // Стоп-условие — страница вернула меньше лимита.
    // НЕ полагаемся на total — Aspro его присылает не всегда.
    if (items.length < limit) return all;
    page++;
    // Мини-пауза между страницами, чтобы реже ловить 429.
    await sleep(300);
    return fetchPage();
  };
  return fetchPage();
}
```

## Примеры fetch-функций

```typescript
export async function fetchProjects(since?: string): Promise<AsproProject[]> {
  const filter: Record<string, string | number> = {};
  if (since) filter['filter[updated_at]'] = since;
  return paginate<AsproProject>('/api/v1/module/st/project/list', filter);
}

export async function fetchStages(projectId: number): Promise<AsproStage[]> {
  return paginate<AsproStage>('/api/v1/module/st/stages/list', {
    'filter[project_id]': projectId,
  });
}

export async function fetchTasks(
  projectId: number,
  since?: string,
): Promise<AsproTask[]> {
  const filter: Record<string, string | number> = {
    'filter[model_id]': projectId,
  };
  if (since) filter['filter[updated_at]'] = since;
  const list = await paginate<AsproTask>('/api/v1/module/task/tasks/list', filter);
  // Безопасный фильтр по project_id на клиенте — Aspro иногда отдаёт
  // соседние модели.
  return list.filter((t) => String(t.model_id) === String(projectId));
}

export async function getProject(projectId: string): Promise<AsproProject | null> {
  try {
    const res = await api().get(`/api/v1/module/st/project/get/${projectId}`);
    const data = res.data?.response?.items ?? res.data?.response;
    return data ?? null;
  } catch {
    return null;
  }
}
```

## POST — form-urlencoded

axios умеет отправлять form-urlencoded через `URLSearchParams`:

```typescript
import { URLSearchParams } from 'url';

export async function asproPost<T = unknown>(
  endpoint: string,
  body: Record<string, unknown>,
): Promise<T> {
  // Сериализация под Aspro:
  //  bool → '1' / '0'
  //  null/undefined → отбрасываем
  //  всё остальное → String()
  const form = new URLSearchParams();
  for (const [k, v] of Object.entries(body)) {
    if (v === null || v === undefined) continue;
    if (typeof v === 'boolean') {
      form.append(k, v ? '1' : '0');
    } else {
      form.append(k, String(v));
    }
  }
  const res = await api().post(endpoint, form.toString(), {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  });
  return res.data?.response;
}
```

## Кастомные поля — оба формата

```typescript
export function attachCustomFields(
  body: Record<string, unknown>,
  custom: Record<number, unknown>,
): Record<string, unknown> {
  // Aspro в разных модулях игнорирует то cf_, то cf. — шлём оба.
  for (const [fieldId, value] of Object.entries(custom)) {
    body[`cf_${fieldId}`] = value;
    body[`cf.${fieldId}`] = value;
  }
  return body;
}
```

## Что не вошло

- Логирование/метрики — добавь свои (через `client.interceptors.request`).
- Учёт `Retry-After` заголовка — добавь по аналогии с Python-версией.
- Async/parallel-fetching нескольких страниц — Aspro может отдать 429,
  если запрашивать страницы конкурентно. Безопасный паттерн —
  последовательная пагинация с лёгкой паузой.
