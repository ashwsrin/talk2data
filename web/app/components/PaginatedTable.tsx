'use client';
import React, { useState, useMemo } from 'react';

interface PaginatedTableProps {
    columns: string[];
    data: Record<string, unknown>[];
    pageSize?: number;
}

const PaginatedTable: React.FC<PaginatedTableProps> = ({
    columns,
    data,
    pageSize = 20,
}) => {
    const [page, setPage] = useState(0);

    const totalPages = Math.max(1, Math.ceil(data.length / pageSize));
    const pageData = useMemo(
        () => data.slice(page * pageSize, (page + 1) * pageSize),
        [data, page, pageSize]
    );

    return (
        <div className="my-3">
            {/* Table */}
            <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
                <table className="min-w-full text-sm">
                    <thead>
                        <tr className="bg-gray-100 dark:bg-gray-800">
                            {columns.map((col) => (
                                <th
                                    key={col}
                                    className="px-4 py-2.5 text-left font-semibold text-gray-700 dark:text-gray-200 whitespace-nowrap border-b border-gray-200 dark:border-gray-700"
                                >
                                    {col}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {pageData.map((row, rowIdx) => (
                            <tr
                                key={rowIdx}
                                className={
                                    rowIdx % 2 === 0
                                        ? 'bg-white dark:bg-gray-900'
                                        : 'bg-gray-50 dark:bg-gray-850'
                                }
                            >
                                {columns.map((col) => (
                                    <td
                                        key={col}
                                        className="px-4 py-2 text-gray-800 dark:text-gray-300 whitespace-nowrap border-b border-gray-100 dark:border-gray-800"
                                    >
                                        {String(row[col] ?? '')}
                                    </td>
                                ))}
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            {/* Pagination controls */}
            {totalPages > 1 && (
                <div className="flex items-center justify-between mt-2 px-1">
                    <span className="text-xs text-gray-500 dark:text-gray-400">
                        Showing {page * pageSize + 1}–{Math.min((page + 1) * pageSize, data.length)} of {data.length} rows
                    </span>
                    <div className="flex items-center gap-1">
                        <button
                            type="button"
                            disabled={page === 0}
                            onClick={() => setPage((p) => Math.max(0, p - 1))}
                            className="px-2.5 py-1 text-xs rounded bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                            ◀ Prev
                        </button>
                        <span className="text-xs text-gray-500 dark:text-gray-400 px-2">
                            {page + 1} / {totalPages}
                        </span>
                        <button
                            type="button"
                            disabled={page >= totalPages - 1}
                            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                            className="px-2.5 py-1 text-xs rounded bg-gray-200 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-300 dark:hover:bg-gray-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                        >
                            Next ▶
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
};

export default PaginatedTable;
