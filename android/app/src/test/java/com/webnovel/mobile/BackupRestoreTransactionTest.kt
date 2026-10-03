package com.webnovel.mobile

import java.io.File
import java.io.IOException
import java.nio.file.Files
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Test

class BackupRestoreTransactionTest {
    private open class FileOps : Backup.RestoreFileOps {
        override fun exists(file: File) = file.exists()
        override fun rename(from: File, to: File) = from.renameTo(to)
        override fun delete(file: File) = !file.exists() || file.delete()
        override fun write(file: File, bytes: ByteArray) {
            file.outputStream().use { it.write(bytes) }
        }
        override fun copy(from: File, to: File) {
            from.inputStream().use { input -> to.outputStream().use { input.copyTo(it) } }
        }
    }

    @Test fun failedSecondCommitRestoresEveryOriginalFile() {
        val root = Files.createTempDirectory("backup-restore-rollback").toFile()
        try {
            val first = File(root, "first.json").apply { writeBytes("old-1".toByteArray()) }
            val second = File(root, "second.json").apply { writeBytes("old-2".toByteArray()) }
            var failed = false
            val ops = object : FileOps() {
                override fun rename(from: File, to: File): Boolean {
                    if (!failed && to == second && from.name.contains(".restore-")) {
                        failed = true
                        return false
                    }
                    return super.rename(from, to)
                }
            }
            val txn = File(root, "transaction")
            val failure = runCatching {
                Backup.commitRestorePlan(
                    listOf(first to "new-1".toByteArray(), second to "new-2".toByteArray()),
                    txn,
                    ops,
                )
            }.exceptionOrNull()

            assertTrue("restore should fail", failure is IOException)
            assertTrue(failure!!.message.orEmpty().contains("已回滚"))
            assertArrayEquals("old-1".toByteArray(), first.readBytes())
            assertArrayEquals("old-2".toByteArray(), second.readBytes())
            assertFalse("successful rollback should clean transaction data", txn.exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun rollbackFailureKeepsSnapshotAndReportsRecoveryDirectory() {
        val root = Files.createTempDirectory("backup-restore-evidence").toFile()
        try {
            val target = File(root, "state.json").apply { writeBytes("old".toByteArray()) }
            val txn = File(root, "transaction")
            var commitFailed = false
            val ops = object : FileOps() {
                override fun rename(from: File, to: File): Boolean {
                    if (to == target && from.name.contains(".restore-")) {
                        commitFailed = true
                        return false
                    }
                    if (commitFailed && from.parentFile == txn && to == target) return false
                    return super.rename(from, to)
                }
            }
            val failure = runCatching {
                Backup.commitRestorePlan(listOf(target to "new".toByteArray()), txn, ops)
            }.exceptionOrNull()

            assertTrue("restore should fail", failure is IOException)
            assertTrue(failure!!.message.orEmpty().contains("回滚失败"))
            assertTrue(failure.message.orEmpty().contains(txn.name))
            assertTrue("rollback evidence must remain on disk", txn.exists())
            assertArrayEquals("old".toByteArray(), File(txn, "0.old").readBytes())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun startupRecoveryRevertsStartedEntriesAndLeavesUntouchedEntries() {
        val root = Files.createTempDirectory("backup-restore-startup").toFile()
        try {
            val mangaDir = File(root, "manga").apply { mkdirs() }
            val changed = File(mangaDir, "_favorites.json").apply {
                writeBytes("new favorites".toByteArray())
            }
            val untouched = File(root, "book_progress.json").apply {
                writeBytes("current progress".toByteArray())
            }
            val transaction = File(root, ".restore-crashed").apply { mkdirs() }
            File(transaction, "0.old").writeBytes("old favorites".toByteArray())
            File(transaction, "0.started").createNewFile()
            File(transaction, "journal.json").writeText(
                JSONObject().put("version", 1).put("entries", JSONArray().apply {
                    put(JSONObject().put("path", "manga/_favorites.json").put("existed", true))
                    put(JSONObject().put("path", "book_progress.json").put("existed", true))
                }).toString(),
            )

            val issues = Backup.recoverInterruptedRestores(root)

            assertTrue("recovery should be clean: $issues", issues.isEmpty())
            assertArrayEquals("old favorites".toByteArray(), changed.readBytes())
            assertArrayEquals("current progress".toByteArray(), untouched.readBytes())
            assertFalse("completed transaction should be removed", transaction.exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun startupRecoveryNeverRollsBackTransactionWithCommittedMarker() {
        val root = Files.createTempDirectory("backup-restore-committed").toFile()
        try {
            val target = File(root, "manga/_favorites.json").apply {
                parentFile?.mkdirs()
                writeBytes("committed favorites".toByteArray())
            }
            val transaction = File(root, ".restore-cleanup-interrupted").apply { mkdirs() }
            File(transaction, "0.old").writeBytes("previous favorites".toByteArray())
            File(transaction, "0.started").createNewFile()
            File(transaction, "committed").writeText("RESTORE_COMMITTED_V1")
            File(transaction, "journal.json").writeText(
                JSONObject().put("version", 1).put("entries", JSONArray().apply {
                    put(JSONObject().put("path", "manga/_favorites.json").put("existed", true))
                }).toString(),
            )

            val issues = Backup.recoverInterruptedRestores(root)

            assertTrue("committed restore needs cleanup only: $issues", issues.isEmpty())
            assertArrayEquals("committed data must remain authoritative",
                "committed favorites".toByteArray(), target.readBytes())
            assertFalse("stale transaction should be cleaned when possible", transaction.exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun startupRecoveryBlocksEngineAndKeepsEvidenceWhenSnapshotIsMissing() {
        val root = Files.createTempDirectory("backup-restore-startup-error").toFile()
        try {
            val transaction = File(root, ".restore-incomplete").apply { mkdirs() }
            File(transaction, "0.started").createNewFile()
            File(transaction, "journal.json").writeText(
                JSONObject().put("version", 1).put("entries", JSONArray().apply {
                    put(JSONObject().put("path", "manga/_history.json").put("existed", true))
                }).toString(),
            )
            val issues = Backup.recoverInterruptedRestores(root)
            assertEquals(1, issues.size)
            assertTrue(issues.single().contains("manga/_history.json"))
            assertTrue("evidence must remain for manual recovery", transaction.exists())
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun startupRecoveryRejectsCorruptSnapshotAndPreservesEvidence() {
        val root = Files.createTempDirectory("backup-restore-corrupt-snapshot").toFile()
        try {
            val mangaDir = File(root, "manga").apply { mkdirs() }
            val target = File(mangaDir, "_history.json").apply {
                writeBytes("partially restored".toByteArray())
            }
            val transaction = File(root, ".restore-corrupt").apply { mkdirs() }
            val corruptSnapshot = "corrupt old snapshot".toByteArray()
            File(transaction, "0.old").writeBytes(corruptSnapshot)
            File(transaction, "0.started").createNewFile()
            val expected = java.security.MessageDigest.getInstance("SHA-256")
                .digest("original history".toByteArray())
                .joinToString("") { "%02x".format(it) }
            File(transaction, "journal.json").writeText(
                JSONObject().put("version", 1).put("entries", JSONArray().apply {
                    put(JSONObject().put("path", "manga/_history.json")
                        .put("existed", true).put("old_sha256", expected))
                }).toString(),
            )

            val issues = Backup.recoverInterruptedRestores(root)

            assertEquals(1, issues.size)
            assertTrue("recovery must report checksum mismatch", issues.single()
                .contains("快照 SHA-256 不匹配"))
            assertArrayEquals("do not overwrite target with corrupt snapshot",
                "partially restored".toByteArray(), target.readBytes())
            assertArrayEquals("preserve corrupt snapshot for manual recovery",
                corruptSnapshot, File(transaction, "0.old").readBytes())
            assertTrue("preserve transaction journal", File(transaction, "journal.json").isFile)
            assertTrue("preserve recovery directory", transaction.isDirectory)
        } finally {
            root.deleteRecursively()
        }
    }

    @Test fun startupRecoveryRecognizesMarkerBeforeOriginalRename() {
        val root = Files.createTempDirectory("backup-restore-pre-rename").toFile()
        try {
            val mangaDir = File(root, "manga").apply { mkdirs() }
            val target = File(mangaDir, "_library.json").apply {
                writeBytes("untouched original".toByteArray())
            }
            val transaction = File(root, ".restore-before-rename").apply { mkdirs() }
            File(transaction, "0.started").createNewFile()
            val digest = java.security.MessageDigest.getInstance("SHA-256")
                .digest(target.readBytes()).joinToString("") { "%02x".format(it) }
            File(transaction, "journal.json").writeText(
                JSONObject().put("version", 1).put("entries", JSONArray().apply {
                    put(JSONObject().put("path", "manga/_library.json")
                        .put("existed", true).put("old_sha256", digest))
                }).toString(),
            )

            val issues = Backup.recoverInterruptedRestores(root)

            assertTrue("original is intact; recovery should succeed: $issues", issues.isEmpty())
            assertArrayEquals("untouched original".toByteArray(), target.readBytes())
            assertFalse(transaction.exists())
        } finally {
            root.deleteRecursively()
        }
    }
}
