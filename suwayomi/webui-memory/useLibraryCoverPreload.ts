/*
 * Copyright (C) Contributors to the Suwayomi project
 *
 * This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at https://mozilla.org/MPL/2.0/.
 */

import { useEffect } from 'react';
import gql from 'graphql-tag';
import { requestManager } from '@/lib/requests/RequestManager.ts';
import { coverMemoryStore, CoverMemoryStore } from '@/features/library/services/CoverMemoryStore.ts';
import type { MangaThumbnailInfo } from '@/features/manga/Manga.types.ts';
import { Mangas } from '@/features/manga/services/Mangas.ts';

export const GET_LIBRARY_COVER_METADATA = gql`
    query GET_LIBRARY_COVER_METADATA {
        mangas(condition: { inLibrary: true }) {
            nodes {
                id
                thumbnailUrl
                thumbnailUrlLastFetched
                sourceId
                inLibrary
            }
        }
    }
`;

export interface LibraryCoverMetadataQuery {
    mangas: {
        nodes: Array<MangaThumbnailInfo & { id: number; inLibrary: boolean }>;
    };
}

export function useLibraryCoverPreload(isForegroundReady: boolean = true) {
    const { data } = requestManager.useGetMangas<LibraryCoverMetadataQuery, { condition: { inLibrary: boolean } }>(
        GET_LIBRARY_COVER_METADATA,
        { condition: { inLibrary: true } },
        { skip: !isForegroundReady },
    );

    useEffect(() => {
        if (!data?.mangas?.nodes) {
            return;
        }

        const nodes = data.mangas.nodes;
        const validUrls = nodes
            .filter((m) => m?.inLibrary && m?.thumbnailUrl)
            .map((m) => Mangas.getThumbnailUrl(m))
            .filter((url: string) => CoverMemoryStore.isAllowedCoverUrl(url, requestManager.getBaseUrl()));

        coverMemoryStore.reconcile(validUrls);

        coverMemoryStore.enqueue(validUrls);
    }, [data]);
}
