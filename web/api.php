<?php
/**
 * GoodNotes 5MB Splitter - Server-side API / CLI Runner
 * PHP 8.0+ compatible, stdlib only (ZipArchive).
 */

declare(strict_types=1);

namespace GoodNotesSplitter;

use ZipArchive;

class SplitterApi
{
    public static function handleRequest(): void
    {
        header('Content-Type: application/json; charset=utf-8');

        if ($_SERVER['REQUEST_METHOD'] === 'GET') {
            echo json_encode([
                'status' => 'ok',
                'service' => 'GoodNotes 5MB Splitter API',
                'version' => '1.0.0',
                'php_version' => PHP_VERSION,
            ], JSON_UNESCAPED_UNICODE);
            return;
        }

        if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
            http_response_code(405);
            echo json_encode(['error' => 'Method Not Allowed'], JSON_UNESCAPED_UNICODE);
            return;
        }

        if (empty($_FILES['file']) || $_FILES['file']['error'] !== UPLOAD_ERR_OK) {
            http_response_code(400);
            echo json_encode(['error' => 'No file uploaded or upload error'], JSON_UNESCAPED_UNICODE);
            return;
        }

        $tmpFile = $_FILES['file']['tmp_name'];
        $origName = $_FILES['file']['name'];

        if (!str_ends_with(strtolower($origName), '.goodnotes')) {
            http_response_code(400);
            echo json_encode(['error' => 'File must be a .goodnotes archive'], JSON_UNESCAPED_UNICODE);
            return;
        }

        // Return basic metadata
        $zip = new ZipArchive();
        if ($zip->open($tmpFile) !== true) {
            http_response_code(400);
            echo json_encode(['error' => 'Failed to open ZIP archive'], JSON_UNESCAPED_UNICODE);
            return;
        }

        $entries = $zip->numFiles;
        $zip->close();

        echo json_encode([
            'status' => 'ok',
            'filename' => $origName,
            'size' => filesize($tmpFile),
            'zip_entries' => $entries,
            'message' => 'Upload verified. Client-side JS engine performs zero-latency local splitting.',
        ], JSON_UNESCAPED_UNICODE);
    }
}

if (php_sapi_name() !== 'cli') {
    SplitterApi::handleRequest();
}
