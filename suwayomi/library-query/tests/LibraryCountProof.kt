/*
 * Copyright (C) Contributors to the mobile-server project
 * SPDX-License-Identifier: MPL-2.0
 */

import graphql.ExecutionInput
import graphql.GraphQL
import graphql.GraphQLContext
import graphql.schema.idl.RuntimeWiring
import graphql.schema.idl.SchemaGenerator
import graphql.schema.idl.SchemaParser
import kotlinx.serialization.json.JsonObject
import org.dataloader.DataLoaderFactory
import org.dataloader.DataLoaderRegistry
import org.jetbrains.exposed.v1.jdbc.Database
import org.jetbrains.exposed.v1.jdbc.SchemaUtils
import org.jetbrains.exposed.v1.jdbc.insert
import org.jetbrains.exposed.v1.jdbc.selectAll
import org.jetbrains.exposed.v1.jdbc.transactions.transaction
import suwayomi.tachidesk.graphql.dataLoaders.ChapterFlagCountForMangaDataLoader
import suwayomi.tachidesk.graphql.dataLoaders.ChaptersForMangaDataLoader
import suwayomi.tachidesk.graphql.server.primitives.GraphQLCursor
import suwayomi.tachidesk.graphql.types.ChapterNodeList
import suwayomi.tachidesk.graphql.types.MangaType
import suwayomi.tachidesk.manga.model.table.ChapterTable
import suwayomi.tachidesk.manga.model.table.MangaTable
import suwayomi.tachidesk.server.ServerConfig
import suwayomi.tachidesk.server.settings.SettingsRegistry
import suwayomi.tachidesk.server.util.ConfigTypeRegistration
import xyz.nulldev.ts.config.GlobalConfigManager
import java.util.concurrent.CompletableFuture

private var cases = 0

private fun addChapter(manga: Int, number: Int, read: Boolean = false) = transaction {
    ChapterTable.insert {
        it[ChapterTable.manga] = manga
        it[url] = "fixture-$manga-$number"
        it[name] = "Fixture chapter $number"
        it[sourceOrder] = number
        it[isRead] = read
        it[isDownloaded] = manga == 1 && number == 1
        it[isBookmarked] = manga == 1 && number <= 2
        it[memo] = JsonObject(emptyMap())
    }
}

private fun schema(mangas: Map<Int, MangaType>): GraphQL {
    val types = SchemaParser().parse("""
        scalar Cursor
        type Query { manga(id: Int!): Manga! }
        type Mutation { add(id: Int!, number: Int!): Manga! }
        type Manga { id: Int!, chapters: ChapterConnection!, unreadCount: Int!, downloadCount: Int!, bookmarkCount: Int! }
        type ChapterConnection { totalCount: Int!, nodes: [Chapter!]!, edges: [Edge!]!, pageInfo: PageInfo! }
        type Chapter { id: Int! }
        type Edge { node: Chapter! }
        type PageInfo { startCursor: Cursor!, endCursor: Cursor!, hasNextPage: Boolean!, hasPreviousPage: Boolean! }
    """.trimIndent())
    val wiring = RuntimeWiring.newRuntimeWiring()
        .scalar(GraphQLCursor)
        .type("Query") { it.dataFetcher("manga") { e -> mangas.getValue(e.getArgument<Int>("id")!!) } }
        .type("Mutation") { it.dataFetcher("add") { e ->
            val id = e.getArgument<Int>("id")!!
            addChapter(id, e.getArgument<Int>("number")!!)
            MangaType.clearCacheFor(id, e)
            mangas.getValue(id)
        } }
        .type("Manga") { it
            .dataFetcher("chapters") { e -> e.getSource<MangaType>()!!.chapters(e) }
            .dataFetcher("unreadCount") { e -> e.getSource<MangaType>()!!.unreadCount(e) }
            .dataFetcher("downloadCount") { e -> e.getSource<MangaType>()!!.downloadCount(e) }
            .dataFetcher("bookmarkCount") { e -> e.getSource<MangaType>()!!.bookmarkCount(e) }
        }
        .build()
    return GraphQL.newGraphQL(SchemaGenerator().makeExecutableSchema(types, wiring)).build()
}

private fun run(graph: GraphQL, query: String, forbidRows: Boolean = true): Map<String, Any?> {
    val registry = DataLoaderRegistry()
        .register("ChapterFlagCountForMangaDataLoader", ChapterFlagCountForMangaDataLoader().getDataLoader(GraphQLContext.getDefault()))
        .register("ChaptersForMangaDataLoader", if (forbidRows) {
            DataLoaderFactory.newDataLoader<Int, ChapterNodeList> { _ ->
                CompletableFuture.failedFuture(AssertionError("Count-only selection loaded full chapter rows"))
            }
        } else {
            ChaptersForMangaDataLoader().getDataLoader(GraphQLContext.getDefault())
        })
    val result = graph.execute(ExecutionInput.newExecutionInput().query(query).dataLoaderRegistry(registry).build())
    check(result.errors.isEmpty()) { result.errors.toString() }
    cases++
    return result.getData()
}

@Suppress("UNCHECKED_CAST")
private fun Map<String, Any?>.objectAt(key: String): Map<String, Any?> = this[key] as Map<String, Any?>

fun main() {
    SettingsRegistry.clear()
    ConfigTypeRegistration.registerCustomTypes()
    GlobalConfigManager.registerModule(ServerConfig.register { GlobalConfigManager.config })
    Database.connect("jdbc:h2:mem:library_count_proof;DB_CLOSE_DELAY=-1;TRUNCATE_LARGE_LENGTH=TRUE", driver = "org.h2.Driver")
    transaction {
        SchemaUtils.create(MangaTable, ChapterTable)
        for (i in 1..4) MangaTable.insert {
            it[id] = i
            it[url] = "fixture-$i"
            it[title] = "Fixture $i"
            it[sourceReference] = 1
            it[memo] = JsonObject(emptyMap())
        }
    }
    for (i in 1..5) addChapter(1, i, i % 2 == 0)
    for (i in 1..2) addChapter(3, i, true)
    for (i in 1..12) addChapter(4, i)
    val mangas = transaction { MangaTable.selectAll().associate { it[MangaTable.id].value to MangaType(it) } }
    val graph = schema(mangas)
    fun count(query: String, outer: String = "manga", inner: String = "chapters", field: String = "totalCount") =
        run(graph, query).objectAt(outer).objectAt(inner)[field]

    check(count("{manga(id:1){chapters{totalCount __typename}}}") == 5)
    check(count("{manga(id:2){chapters{totalCount}}}") == 0)
    check(count("{manga(id:3){chapters{totalCount}}}") == 2)
    check(count("{manga(id:1){chapters{n:totalCount}}}", field = "n") == 5)
    check(count("query{manga(id:1){...Fields}} fragment Fields on Manga{chapters{totalCount}}") == 5)
    check(count("{manga(id:1){chapters{... on ChapterConnection{totalCount}}}}") == 5)
    check(count("{manga(id:1){chapters{totalCount nodes @skip(if:true){id}}}}") == 5)
    check(count("{manga(id:1){chapters{totalCount nodes @include(if:false){id}}}}") == 5)
    val batch = run(graph, "{c:manga(id:3){chapters{totalCount}} e:manga(id:2){chapters{totalCount}} a:manga(id:1){chapters{totalCount}}}")
    check(batch.objectAt("c").objectAt("chapters")["totalCount"] == 2)
    check(batch.objectAt("e").objectAt("chapters")["totalCount"] == 0)
    check(batch.objectAt("a").objectAt("chapters")["totalCount"] == 5)
    val flags = run(graph, "{manga(id:1){unreadCount downloadCount bookmarkCount chapters{totalCount}}}").objectAt("manga")
    check(flags["unreadCount"] == 3 && flags["downloadCount"] == 1 && flags["bookmarkCount"] == 2)
    val full = run(graph, "{manga(id:1){count:chapters{totalCount} full:chapters{totalCount nodes{id} edges{node{id}} pageInfo{endCursor}}}}", false).objectAt("manga")
    check(full.objectAt("count")["totalCount"] == 5)
    check((full.objectAt("full")["nodes"] as List<*>).size == 5)
    check((full.objectAt("full")["edges"] as List<*>).size == 2)
    check(full.objectAt("full").objectAt("pageInfo")["endCursor"] == "4")
    val merged = run(graph, "{manga(id:1){c:chapters{totalCount} c:chapters{nodes{id}}}}", false).objectAt("manga").objectAt("c")
    check(merged["totalCount"] == 5 && (merged["nodes"] as List<*>).size == 5)
    val conditional = run(graph, "{manga(id:1){chapters{totalCount nodes @include(if:true){id}}}}", false).objectAt("manga").objectAt("chapters")
    check((conditional["nodes"] as List<*>).size == 5)
    val empty = run(graph, "{manga(id:2){chapters{totalCount nodes{id} edges{node{id}}}}}", false).objectAt("manga").objectAt("chapters")
    check(empty["totalCount"] == 0 && (empty["nodes"] as List<*>).isEmpty())
    check((empty["edges"] as List<*>).isEmpty())
    val page = run(graph, "{manga(id:1){chapters{pageInfo{endCursor}}}}", false).objectAt("manga").objectAt("chapters")
    check(page.objectAt("pageInfo")["endCursor"] == "4")
    addChapter(1, 6)
    check(count("{manga(id:1){chapters{totalCount}}}") == 6)
    val mutation = run(graph, "mutation{a:add(id:2,number:1){chapters{totalCount}} b:add(id:2,number:2){chapters{totalCount}}}")
    check(mutation.objectAt("a").objectAt("chapters")["totalCount"] == 1)
    check(mutation.objectAt("b").objectAt("chapters")["totalCount"] == 2)
    println("Library count proof: $cases GraphQL/H2 cases passed")
}
