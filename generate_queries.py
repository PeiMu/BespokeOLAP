#!/usr/bin/env python3
"""Generate all 113 JOB query C++ implementations."""
import os

OUT = "output"

def W(name, code):
    with open(os.path.join(OUT, name), 'w') as f:
        f.write(code)

# Skip Q1a-Q1d, Q2a-Q2d (already implemented manually)

# ============================================================================
# Helper to build file wrapper
# ============================================================================
def wrap(func, parse, body):
    return (
        '#include "query_impl.hpp"\n'
        '#include "args_parser.hpp"\n\n'
        'QueryResult ' + func + '(const Database& db, const QueryRequest& req, int run_nr) {\n'
        '    (void)run_nr;\n'
        '    auto args = ' + parse + '(req);\n'
        '    (void)args;\n\n'
        + body +
        '}\n'
    )

# ============================================================================
# Q3: keyword LIKE '%sequel%', movie_info.info IN (...), year > X
# ============================================================================
def q3(v, infos, yr):
    info_check = " || ".join(['info_sv == "' + i + '"' for i in infos])
    return wrap("run_q3"+v, "parse_q3"+v, '''
    std::vector<int32_t> seq_kids;
    for (const auto& [kw, kid] : db.keyword.keyword_to_id)
        if (kw.find("sequel") != std::string::npos) seq_kids.push_back(kid);

    std::vector<int32_t> kw_movies;
    for (int32_t kid : seq_kids) {
        auto it = db.movie_keyword.keyword_to_movies.find(kid);
        if (it != db.movie_keyword.keyword_to_movies.end())
            kw_movies.insert(kw_movies.end(), it->second.begin(), it->second.end());
    }
    std::sort(kw_movies.begin(), kw_movies.end());
    kw_movies.erase(std::unique(kw_movies.begin(), kw_movies.end()), kw_movies.end());

    std::string min_title;
    bool has = false;

    for (int32_t mid : kw_movies) {
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py <= ''' + str(yr) + ''') continue;

        bool found_mi = false;
        for (const auto& [pit_id, part] : db.movie_info.partitions) {
            uint32_t b = part.movie_csr.begin(mid), e = part.movie_csr.end(mid);
            for (uint32_t i = b; i < e; ++i) {
                auto info_sv = part.info.get(i);
                if (''' + info_check + ''') { found_mi = true; break; }
            }
            if (found_mi) break;
        }
        if (!found_mi) continue;
        update_min_str(min_title, db.title.get_title(mid), has);
    }

    return {{"movie_title"}, {{has ? min_title : ""}}};
''')

W("query_q3a.cpp", q3("a", ["Sweden","Norway","Germany","Denmark","Swedish","Denish","Norwegian","German"], 2005))
W("query_q3b.cpp", q3("b", ["Bulgaria"], 2010))
W("query_q3c.cpp", q3("c", ["Sweden","Norway","Germany","Denmark","Swedish","Denish","Norwegian","German","USA","American"], 1990))

# ============================================================================
# Q4: rating + keyword LIKE '%sequel%' + mi_idx.info > X + year > Y
# ============================================================================
def q4(v, gt, yr):
    return wrap("run_q4"+v, "parse_q4"+v, '''
    int32_t rating_it = db.info_type.rating_id;
    auto pit = db.movie_info_idx.partitions.find(rating_it);
    if (pit == db.movie_info_idx.partitions.end())
        return {{"rating", "movie_title"}, {{"", ""}}};
    const auto& midx = pit->second;

    std::vector<int32_t> seq_kids;
    for (const auto& [kw, kid] : db.keyword.keyword_to_id)
        if (kw.find("sequel") != std::string::npos) seq_kids.push_back(kid);
    std::vector<int32_t> kw_movies;
    for (int32_t kid : seq_kids) {
        auto it = db.movie_keyword.keyword_to_movies.find(kid);
        if (it != db.movie_keyword.keyword_to_movies.end())
            kw_movies.insert(kw_movies.end(), it->second.begin(), it->second.end());
    }
    std::sort(kw_movies.begin(), kw_movies.end());
    kw_movies.erase(std::unique(kw_movies.begin(), kw_movies.end()), kw_movies.end());

    std::string min_r, min_t;
    bool hr = false, ht = false;

    for (int32_t mid : kw_movies) {
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py <= ''' + str(yr) + ''') continue;
        uint32_t b = midx.movie_csr.begin(mid), e = midx.movie_csr.end(mid);
        for (uint32_t i = b; i < e; ++i) {
            auto sv = midx.info.get(i);
            if (sv > "''' + gt + '''") {
                update_min_str(min_r, sv, hr);
                update_min_str(min_t, db.title.get_title(mid), ht);
            }
        }
    }
    return {{"rating", "movie_title"}, {{hr ? min_r : "", ht ? min_t : ""}}};
''')

W("query_q4a.cpp", q4("a","5.0",2005))
W("query_q4b.cpp", q4("b","9.0",2010))
W("query_q4c.cpp", q4("c","2.0",1990))

# ============================================================================
# Q5: production companies + mc note + movie_info IN (...) + year
# ============================================================================
def q5(v, mc_likes, mc_not_likes, infos, yr, hdr):
    like_c = " && ".join(['like_match(note_sv, "'+p+'")' for p in mc_likes])
    nlike_c = " || ".join(['like_match(note_sv, "'+p+'")' for p in mc_not_likes]) if mc_not_likes else "false"
    info_c = " || ".join(['info_sv == "'+i+'"' for i in infos])
    return wrap("run_q5"+v, "parse_q5"+v, '''
    int32_t ct_id = db.company_type.production_companies_id;
    std::string min_t; bool ht = false;

    for (uint32_t mc_i = 0; mc_i < db.movie_companies.num_rows; ++mc_i) {
        if (db.movie_companies.company_type_id[mc_i] != ct_id) continue;
        if (db.movie_companies.note.is_null(mc_i)) continue;
        auto note_sv = db.movie_companies.note.get(mc_i);
        if (!(''' + like_c + ''')) continue;
        if (''' + nlike_c + ''') continue;

        int32_t mid = db.movie_companies.movie_id[mc_i];
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py <= ''' + str(yr) + ''') continue;

        bool fm = false;
        for (const auto& [pit_id, part] : db.movie_info.partitions) {
            uint32_t b = part.movie_csr.begin(mid), e = part.movie_csr.end(mid);
            for (uint32_t i = b; i < e; ++i) {
                auto info_sv = part.info.get(i);
                if (''' + info_c + ''') { fm = true; break; }
            }
            if (fm) break;
        }
        if (!fm) continue;
        update_min_str(min_t, db.title.get_title(mid), ht);
    }
    return {{"''' + hdr + '''"}, {{ht ? min_t : ""}}};
''')

W("query_q5a.cpp", q5("a",["%(theatrical)%","%(France)%"],[],["Sweden","Norway","Germany","Denmark","Swedish","Denish","Norwegian","German"],2005,"typical_european_movie"))
W("query_q5b.cpp", q5("b",["%(VHS)%","%(USA)%","%(1994)%"],[],["USA","America"],2010,"american_vhs_movie"))
W("query_q5c.cpp", q5("c",["%(USA)%"],["%(TV)%"],["Sweden","Norway","Germany","Denmark","Swedish","Denish","Norwegian","German","USA","American"],1990,"american_movie"))

# ============================================================================
# Q6: cast_info + keyword + movie_keyword + name + title
# Strategy: get movie_ids from keyword, then for each movie check cast_info for name matches
# ============================================================================
def q6(v, kws, name_like, yr, h3):
    if len(kws) == 1:
        kw_code = '''
    auto kit = db.keyword.keyword_to_id.find("''' + kws[0] + '''");
    if (kit == db.keyword.keyword_to_id.end()) return {h, {{"","",""}}};
    std::vector<int32_t> kw_movies;
    auto mki = db.movie_keyword.keyword_to_movies.find(kit->second);
    if (mki != db.movie_keyword.keyword_to_movies.end()) kw_movies = mki->second;
'''
    else:
        kw_list = "{" + ",".join(['"'+k+'"' for k in kws]) + "}"
        kw_code = '''
    std::vector<std::string> kws = ''' + kw_list + ''';
    std::vector<int32_t> kw_movies;
    for (const auto& kw : kws) {
        auto kit = db.keyword.keyword_to_id.find(kw);
        if (kit == db.keyword.keyword_to_id.end()) continue;
        auto mki = db.movie_keyword.keyword_to_movies.find(kit->second);
        if (mki != db.movie_keyword.keyword_to_movies.end())
            kw_movies.insert(kw_movies.end(), mki->second.begin(), mki->second.end());
    }
    std::sort(kw_movies.begin(), kw_movies.end());
    kw_movies.erase(std::unique(kw_movies.begin(), kw_movies.end()), kw_movies.end());
'''

    name_check = ''
    if name_like:
        name_check = '            if (!like_match(db.name.get_name(pid), "' + name_like + '")) continue;\n'

    return wrap("run_q6"+v, "parse_q6"+v, '''
    std::vector<std::string> h = {"movie_keyword", "actor_name", "''' + h3 + '''"};
''' + kw_code + '''
    std::string mk, mn, mt; bool hk=false, hn=false, htl=false;
    for (int32_t mid : kw_movies) {
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py <= ''' + str(yr) + ''') continue;
        uint32_t cb = db.cast_info.movie_csr.begin(mid), ce = db.cast_info.movie_csr.end(mid);
        for (uint32_t ci = cb; ci < ce; ++ci) {
            int32_t pid = db.cast_info.person_id[ci];
            if (!db.name.valid_id(pid)) continue;
''' + name_check + '''            update_min_str(mn, db.name.get_name(pid), hn);
            update_min_str(mt, db.title.get_title(mid), htl);
            uint32_t mb = db.movie_keyword.movie_csr.begin(mid), me = db.movie_keyword.movie_csr.end(mid);
            for (uint32_t mi = mb; mi < me; ++mi)
                update_min_str(mk, db.keyword.get_keyword(db.movie_keyword.keyword_id[mi]), hk);
        }
    }
    return {h, {{hk?mk:"", hn?mn:"", htl?mt:""}}};
''')

W("query_q6a.cpp", q6("a",["marvel-cinematic-universe"],"%Downey%Robert%",2010,"marvel_movie"))
W("query_q6b.cpp", q6("b",["superhero","sequel","second-part","marvel-comics","based-on-comic","tv-special","fight","violence"],"%Downey%Robert%",2014,"hero_movie"))
W("query_q6c.cpp", q6("c",["marvel-cinematic-universe"],"%Downey%Robert%",2014,"marvel_movie"))
W("query_q6d.cpp", q6("d",["superhero","sequel","second-part","marvel-comics","based-on-comic","tv-special","fight","violence"],"%Downey%Robert%",2000,"hero_movie"))
W("query_q6e.cpp", q6("e",["marvel-cinematic-universe"],"%Downey%Robert%",2000,"marvel_movie"))
W("query_q6f.cpp", q6("f",["superhero","sequel","second-part","marvel-comics","based-on-comic","tv-special","fight","violence"],None,2000,"hero_movie"))

# ============================================================================
# Q7: Complex person-info queries - write each manually
# ============================================================================
# Q7a
W("query_q7a.cpp", wrap("run_q7a","parse_q7a",'''
    int32_t it_id = db.info_type.mini_biography_id;
    int32_t lt_id = -1;
    for (const auto& [link, lid] : db.link_type.link_to_id)
        if (link == "features") { lt_id = lid; break; }
    if (lt_id == -1) return {{"of_person","biography_movie"},{{"",""}}};

    auto pi_it = db.person_info.partitions.find(it_id);
    if (pi_it == db.person_info.partitions.end()) return {{"of_person","biography_movie"},{{"",""}}};
    const auto& pip = pi_it->second;

    std::vector<int32_t> cands;
    for (uint32_t i = 0; i < pip.person_id.size(); ++i)
        if (!pip.note.is_null(i) && pip.note.get(i) == "Volker Boehm")
            cands.push_back(pip.person_id[i]);

    std::string mn, mt; bool hn=false, ht=false;
    for (int32_t pid : cands) {
        if (!db.name.valid_id(pid)) continue;
        auto np = db.name.get_name_pcode_cf(pid);
        if (np < "A" || np >= "G") continue;
        uint8_t g = db.name.get_gender(pid);
        auto nn = db.name.get_name(pid);
        if (g == 1) {}
        else if (g == 2 && like_match(nn, "B%")) {}
        else continue;
        uint32_t ab = db.aka_name.person_csr.begin(pid), ae = db.aka_name.person_csr.end(pid);
        bool fa = false;
        for (uint32_t i = ab; i < ae; ++i)
            if (db.aka_name.name.get(i).find('a') != std::string_view::npos) { fa = true; break; }
        if (!fa) continue;
        auto ci_it = db.cast_info.person_to_rows.find(pid);
        if (ci_it == db.cast_info.person_to_rows.end()) continue;
        for (uint32_t cr : ci_it->second) {
            int32_t mid = db.cast_info.movie_id[cr];
            if (!db.title.valid_id(mid)) continue;
            int32_t py = db.title.get_production_year(mid);
            if (py == NULL_SENTINEL || py < 1980 || py > 1995) continue;
            auto ml_it = db.movie_link.linked_movie_to_rows.find(mid);
            if (ml_it == db.movie_link.linked_movie_to_rows.end()) continue;
            bool fl = false;
            for (uint32_t mr : ml_it->second)
                if (db.movie_link.link_type_id[mr] == lt_id) { fl = true; break; }
            if (!fl) continue;
            update_min_str(mn, nn, hn);
            update_min_str(mt, db.title.get_title(mid), ht);
        }
    }
    return {{"of_person","biography_movie"},{{hn?mn:"",ht?mt:""}}};
'''))

# Q7b
W("query_q7b.cpp", wrap("run_q7b","parse_q7b",'''
    int32_t it_id = db.info_type.mini_biography_id;
    int32_t lt_id = -1;
    for (const auto& [link, lid] : db.link_type.link_to_id)
        if (link == "features") { lt_id = lid; break; }
    if (lt_id == -1) return {{"of_person","biography_movie"},{{"",""}}};
    auto pi_it = db.person_info.partitions.find(it_id);
    if (pi_it == db.person_info.partitions.end()) return {{"of_person","biography_movie"},{{"",""}}};
    const auto& pip = pi_it->second;
    std::vector<int32_t> cands;
    for (uint32_t i = 0; i < pip.person_id.size(); ++i)
        if (!pip.note.is_null(i) && pip.note.get(i) == "Volker Boehm")
            cands.push_back(pip.person_id[i]);
    std::string mn, mt; bool hn=false, ht=false;
    for (int32_t pid : cands) {
        if (!db.name.valid_id(pid)) continue;
        if (!like_match(db.name.get_name_pcode_cf(pid), "D%")) continue;
        if (db.name.get_gender(pid) != 1) continue;
        auto nn = db.name.get_name(pid);
        uint32_t ab = db.aka_name.person_csr.begin(pid), ae = db.aka_name.person_csr.end(pid);
        bool fa = false;
        for (uint32_t i = ab; i < ae; ++i)
            if (db.aka_name.name.get(i).find('a') != std::string_view::npos) { fa = true; break; }
        if (!fa) continue;
        auto ci_it = db.cast_info.person_to_rows.find(pid);
        if (ci_it == db.cast_info.person_to_rows.end()) continue;
        for (uint32_t cr : ci_it->second) {
            int32_t mid = db.cast_info.movie_id[cr];
            if (!db.title.valid_id(mid)) continue;
            int32_t py = db.title.get_production_year(mid);
            if (py == NULL_SENTINEL || py < 1980 || py > 1984) continue;
            auto ml_it = db.movie_link.linked_movie_to_rows.find(mid);
            if (ml_it == db.movie_link.linked_movie_to_rows.end()) continue;
            bool fl = false;
            for (uint32_t mr : ml_it->second)
                if (db.movie_link.link_type_id[mr] == lt_id) { fl = true; break; }
            if (!fl) continue;
            update_min_str(mn, nn, hn);
            update_min_str(mt, db.title.get_title(mid), ht);
        }
    }
    return {{"of_person","biography_movie"},{{hn?mn:"",ht?mt:""}}};
'''))

# Q7c
W("query_q7c.cpp", wrap("run_q7c","parse_q7c",'''
    int32_t it_id = db.info_type.mini_biography_id;
    std::vector<int32_t> lt_ids;
    for (const auto& [link, lid] : db.link_type.link_to_id)
        if (link=="references"||link=="referenced in"||link=="features"||link=="featured in")
            lt_ids.push_back(lid);
    if (lt_ids.empty()) return {{"cast_member_name","cast_member_info"},{{"",""}}};
    auto pi_it = db.person_info.partitions.find(it_id);
    if (pi_it == db.person_info.partitions.end()) return {{"cast_member_name","cast_member_info"},{{"",""}}};
    const auto& pip = pi_it->second;
    std::vector<int32_t> cands;
    for (uint32_t i = 0; i < pip.person_id.size(); ++i)
        if (!pip.note.is_null(i)) cands.push_back(pip.person_id[i]);
    std::sort(cands.begin(), cands.end());
    cands.erase(std::unique(cands.begin(), cands.end()), cands.end());
    std::string mn, mi2; bool hn=false, hi=false;
    for (int32_t pid : cands) {
        if (!db.name.valid_id(pid)) continue;
        auto np = db.name.get_name_pcode_cf(pid);
        if (np < "A" || np >= "G") continue;
        uint8_t g = db.name.get_gender(pid);
        auto nn = db.name.get_name(pid);
        if (g == 1) {}
        else if (g == 2 && like_match(nn, "A%")) {}
        else continue;
        uint32_t ab = db.aka_name.person_csr.begin(pid), ae = db.aka_name.person_csr.end(pid);
        bool fa = false;
        for (uint32_t i = ab; i < ae; ++i) {
            auto an = db.aka_name.name.get(i);
            if (!db.aka_name.name.is_null(i) && (an.find('a')!=std::string_view::npos || like_match(an,"A%")))
            { fa = true; break; }
        }
        if (!fa) continue;
        auto ci_it = db.cast_info.person_to_rows.find(pid);
        if (ci_it == db.cast_info.person_to_rows.end()) continue;
        for (uint32_t cr : ci_it->second) {
            int32_t mid = db.cast_info.movie_id[cr];
            if (!db.title.valid_id(mid)) continue;
            int32_t py = db.title.get_production_year(mid);
            if (py == NULL_SENTINEL || py < 1980 || py > 2010) continue;
            auto ml_it = db.movie_link.linked_movie_to_rows.find(mid);
            if (ml_it == db.movie_link.linked_movie_to_rows.end()) continue;
            bool fl = false;
            for (uint32_t mr : ml_it->second) {
                for (int32_t ok : lt_ids) if (db.movie_link.link_type_id[mr]==ok) { fl=true; break; }
                if (fl) break;
            }
            if (!fl) continue;
            update_min_str(mn, nn, hn);
            uint32_t pb = pip.person_csr.begin(pid), pe = pip.person_csr.end(pid);
            for (uint32_t pi = pb; pi < pe; ++pi)
                if (!pip.note.is_null(pi)) update_min_str(mi2, pip.info.get(pi), hi);
        }
    }
    return {{"cast_member_name","cast_member_info"},{{hn?mn:"",hi?mi2:""}}};
'''))

print("Generated Q3-Q7")

# ============================================================================
# Q8: aka_name, cast_info, company_name, movie_companies, name, role_type, title
# ============================================================================
W("query_q8a.cpp", wrap("run_q8a","parse_q8a",'''
    int32_t rt_id = db.role_type.actress_id;
    std::string ma, mt; bool ha=false, ht=false;
    for (uint32_t ci = 0; ci < db.cast_info.num_rows; ++ci) {
        if (db.cast_info.role_id[ci] != rt_id) continue;
        if (db.cast_info.note.is_null(ci) || db.cast_info.note.get(ci) != "(voice: English version)") continue;
        int32_t pid = db.cast_info.person_id[ci];
        if (!db.name.valid_id(pid)) continue;
        auto nn = db.name.get_name(pid);
        if (!like_match(nn,"%Yo%") || like_match(nn,"%Yu%")) continue;
        int32_t mid = db.cast_info.movie_id[ci];
        if (!db.title.valid_id(mid)) continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        bool fm = false;
        for (uint32_t mi = mb; mi < me; ++mi) {
            int32_t cid = db.movie_companies.company_id[mi];
            if (!db.company_name.valid_id(cid) || db.company_name.get_country_code(cid) != "[jp]") continue;
            if (db.movie_companies.note.is_null(mi)) continue;
            auto mn2 = db.movie_companies.note.get(mi);
            if (like_match(mn2,"%(Japan)%") && !like_match(mn2,"%(USA)%")) { fm = true; break; }
        }
        if (!fm) continue;
        uint32_t ab = db.aka_name.person_csr.begin(pid), ae = db.aka_name.person_csr.end(pid);
        for (uint32_t ai = ab; ai < ae; ++ai) update_min_str(ma, db.aka_name.name.get(ai), ha);
        update_min_str(mt, db.title.get_title(mid), ht);
    }
    return {{"actress_pseudonym","japanese_movie_dubbed"},{{ha?ma:"",ht?mt:""}}};
'''))

W("query_q8b.cpp", wrap("run_q8b","parse_q8b",'''
    int32_t rt_id = db.role_type.actress_id;
    std::string ma, mt; bool ha=false, ht=false;
    for (uint32_t ci = 0; ci < db.cast_info.num_rows; ++ci) {
        if (db.cast_info.role_id[ci] != rt_id) continue;
        if (db.cast_info.note.is_null(ci) || db.cast_info.note.get(ci) != "(voice: English version)") continue;
        int32_t pid = db.cast_info.person_id[ci];
        if (!db.name.valid_id(pid)) continue;
        auto nn = db.name.get_name(pid);
        if (!like_match(nn,"%Yo%") || like_match(nn,"%Yu%")) continue;
        int32_t mid = db.cast_info.movie_id[ci];
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py < 2006 || py > 2007) continue;
        auto ttl = db.title.get_title(mid);
        if (!like_match(ttl,"One Piece%") && !like_match(ttl,"Dragon Ball Z%")) continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        bool fm = false;
        for (uint32_t mi = mb; mi < me; ++mi) {
            int32_t cid = db.movie_companies.company_id[mi];
            if (!db.company_name.valid_id(cid) || db.company_name.get_country_code(cid) != "[jp]") continue;
            if (db.movie_companies.note.is_null(mi)) continue;
            auto mn2 = db.movie_companies.note.get(mi);
            if (like_match(mn2,"%(Japan)%") && !like_match(mn2,"%(USA)%") &&
                (like_match(mn2,"%(2006)%") || like_match(mn2,"%(2007)%"))) { fm = true; break; }
        }
        if (!fm) continue;
        uint32_t ab = db.aka_name.person_csr.begin(pid), ae = db.aka_name.person_csr.end(pid);
        for (uint32_t ai = ab; ai < ae; ++ai) update_min_str(ma, db.aka_name.name.get(ai), ha);
        update_min_str(mt, ttl, ht);
    }
    return {{"acress_pseudonym","japanese_anime_movie"},{{ha?ma:"",ht?mt:""}}};
'''))

# Q8c: writer + [us] company
W("query_q8c.cpp", wrap("run_q8c","parse_q8c",'''
    int32_t rt_id = db.role_type.writer_id;
    std::string ma, mt; bool ha=false, ht=false;
    for (uint32_t ci = 0; ci < db.cast_info.num_rows; ++ci) {
        if (db.cast_info.role_id[ci] != rt_id) continue;
        int32_t pid = db.cast_info.person_id[ci], mid = db.cast_info.movie_id[ci];
        if (!db.title.valid_id(mid)) continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        bool fm = false;
        for (uint32_t mi = mb; mi < me; ++mi) {
            int32_t cid = db.movie_companies.company_id[mi];
            if (db.company_name.valid_id(cid) && db.company_name.get_country_code(cid) == "[us]") { fm=true; break; }
        }
        if (!fm) continue;
        uint32_t ab = db.aka_name.person_csr.begin(pid), ae = db.aka_name.person_csr.end(pid);
        if (ab == ae) continue;
        for (uint32_t ai = ab; ai < ae; ++ai) update_min_str(ma, db.aka_name.name.get(ai), ha);
        update_min_str(mt, db.title.get_title(mid), ht);
    }
    return {{"writer_pseudo_name","movie_title"},{{ha?ma:"",ht?mt:""}}};
'''))

# Q8d: costume designer + [us]
W("query_q8d.cpp", wrap("run_q8d","parse_q8d",'''
    int32_t rt_id = db.role_type.costume_designer_id;
    std::string ma, mt; bool ha=false, ht=false;
    for (uint32_t ci = 0; ci < db.cast_info.num_rows; ++ci) {
        if (db.cast_info.role_id[ci] != rt_id) continue;
        int32_t pid = db.cast_info.person_id[ci], mid = db.cast_info.movie_id[ci];
        if (!db.title.valid_id(mid)) continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        bool fm = false;
        for (uint32_t mi = mb; mi < me; ++mi) {
            int32_t cid = db.movie_companies.company_id[mi];
            if (db.company_name.valid_id(cid) && db.company_name.get_country_code(cid) == "[us]") { fm=true; break; }
        }
        if (!fm) continue;
        uint32_t ab = db.aka_name.person_csr.begin(pid), ae = db.aka_name.person_csr.end(pid);
        if (ab == ae) continue;
        for (uint32_t ai = ab; ai < ae; ++ai) update_min_str(ma, db.aka_name.name.get(ai), ha);
        update_min_str(mt, db.title.get_title(mid), ht);
    }
    return {{"costume_designer_pseudo","movie_with_costumes"},{{ha?ma:"",ht?mt:""}}};
'''))

print("Generated Q8")

# Q9-Q33 are complex. Let me continue generating them...
# Given the file is already very long, I'll generate the rest using a more compact approach

# For Q9-Q33, many follow patterns but with enough variation that I need to write each one.
# I'll use a compact generation approach.

# ============================================================================
# Q9: Voice actress queries with aka_name, char_name, cast_info, company_name, movie_companies, name, role_type, title
# ============================================================================
def q9_base(v, ci_notes, name_like, year_lo, year_hi, mc_extra, hdrs, extra_title="", extra_mc_note=""):
    ci_check = " || ".join(['ci_n == "'+n+'"' for n in ci_notes])
    name_f = ('if (!like_match(nn, "'+name_like+'")) continue;') if name_like else ''
    yr_f = ''
    if year_lo is not None and year_hi is not None:
        yr_f = 'if (py == NULL_SENTINEL || py < '+str(year_lo)+' || py > '+str(year_hi)+') continue;'
    elif year_lo is not None:
        yr_f = 'if (py == NULL_SENTINEL) continue;'

    hdr_str = ",".join(['"'+h+'"' for h in hdrs])
    empty_row = ",".join(['""' for _ in hdrs])

    has_nname = len(hdrs) >= 4  # Q9b-d have 4 headers

    vars_decl = 'std::string ma, mc, mt; bool ha=false, hc=false, htl=false;'
    nname_var = ''
    if has_nname:
        vars_decl = 'std::string ma, mc, mn2, mt; bool ha=false, hc=false, hn2=false, htl=false;'
        nname_var = '            update_min_str(mn2, nn, hn2);'

    row_parts = ['ha?ma:""', 'hc?mc:""']
    if has_nname:
        row_parts.append('hn2?mn2:""')
    row_parts.append('htl?mt:""')
    row_str = ",".join(row_parts)

    return wrap("run_q9"+v, "parse_q9"+v, '''
    int32_t rt_id = db.role_type.actress_id;
    ''' + vars_decl + '''
    for (uint32_t ci = 0; ci < db.cast_info.num_rows; ++ci) {
        if (db.cast_info.role_id[ci] != rt_id) continue;
        if (db.cast_info.note.is_null(ci)) continue;
        auto ci_n = db.cast_info.note.get(ci);
        if (!(''' + ci_check + ''')) continue;
        int32_t pid = db.cast_info.person_id[ci];
        if (!db.name.valid_id(pid)) continue;
        if (db.name.get_gender(pid) != 2) continue;
        auto nn = db.name.get_name(pid);
        ''' + name_f + '''
        int32_t mid = db.cast_info.movie_id[ci];
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        ''' + yr_f + '''
        ''' + extra_title + '''
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        bool fm = false;
        for (uint32_t mi = mb; mi < me; ++mi) {
            int32_t cid = db.movie_companies.company_id[mi];
            if (!db.company_name.valid_id(cid) || db.company_name.get_country_code(cid) != "[us]") continue;
            ''' + mc_extra + '''
            fm = true; break;
        }
        if (!fm) continue;
        int32_t chn_id = db.cast_info.person_role_id[ci];
        if (chn_id == NULL_SENTINEL || !db.char_name.valid_id(chn_id)) continue;
        uint32_t ab = db.aka_name.person_csr.begin(pid), ae = db.aka_name.person_csr.end(pid);
        if (ab == ae) continue;
        for (uint32_t ai = ab; ai < ae; ++ai) update_min_str(ma, db.aka_name.name.get(ai), ha);
        update_min_str(mc, db.char_name.get_name(chn_id), hc);
''' + nname_var + '''
        update_min_str(mt, db.title.get_title(mid), htl);
    }
    return {{''' + hdr_str + '''}, {{''' + row_str + '''}}};
''')

W("query_q9a.cpp", q9_base("a",
    ["(voice)","(voice: Japanese version)","(voice) (uncredited)","(voice: English version)"],
    "%Ang%", 2005, 2015,
    'if (db.movie_companies.note.is_null(mi)) continue; auto mcn=db.movie_companies.note.get(mi); if (!(like_match(mcn,"%(USA)%")||like_match(mcn,"%(worldwide)%"))) continue;',
    ["alternative_name","character_name","movie"]))

W("query_q9b.cpp", q9_base("b",
    ["(voice)"], "%Angel%", 2007, 2010,
    'if (db.movie_companies.note.is_null(mi)) continue; auto mcn=db.movie_companies.note.get(mi); if (!like_match(mcn,"%(200%)%")) continue; if (!(like_match(mcn,"%(USA)%")||like_match(mcn,"%(worldwide)%"))) continue;',
    ["alternative_name","voiced_character","voicing_actress","american_movie"]))

W("query_q9c.cpp", q9_base("c",
    ["(voice)","(voice: Japanese version)","(voice) (uncredited)","(voice: English version)"],
    "%An%", None, None,
    '',
    ["alternative_name","voiced_character_name","voicing_actress","american_movie"]))

W("query_q9d.cpp", q9_base("d",
    ["(voice)","(voice: Japanese version)","(voice) (uncredited)","(voice: English version)"],
    None, None, None,
    '',
    ["alternative_name","voiced_char_name","voicing_actress","american_movie"]))

print("Generated Q9")

# ============================================================================
# Q10: char_name, cast_info, company_name, company_type, movie_companies, role_type, title
# ============================================================================
def q10(v, ci_likes, rt_role, cn_cc, yr, hdrs):
    ci_ck = " && ".join(['like_match(cn2,"'+p+'")' for p in ci_likes])
    return wrap("run_q10"+v, "parse_q10"+v, '''
    int32_t rt_id = -1;
    for (const auto& [r,rid] : db.role_type.role_to_id) if (r=="''' + rt_role + '''") { rt_id=rid; break; }
    std::string mc2, mt; bool hc=false, ht=false;
    for (uint32_t ci = 0; ci < db.cast_info.num_rows; ++ci) {
        if (db.cast_info.role_id[ci] != rt_id) continue;
        if (db.cast_info.note.is_null(ci)) continue;
        auto cn2 = db.cast_info.note.get(ci);
        if (!(''' + ci_ck + ''')) continue;
        int32_t mid = db.cast_info.movie_id[ci];
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py <= ''' + str(yr) + ''') continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        bool fm = false;
        for (uint32_t mi = mb; mi < me; ++mi) {
            int32_t cid = db.movie_companies.company_id[mi];
            if (db.company_name.valid_id(cid) && db.company_name.get_country_code(cid) == "''' + cn_cc + '''") { fm=true; break; }
        }
        if (!fm) continue;
        int32_t ch = db.cast_info.person_role_id[ci];
        if (ch == NULL_SENTINEL || !db.char_name.valid_id(ch)) continue;
        update_min_str(mc2, db.char_name.get_name(ch), hc);
        update_min_str(mt, db.title.get_title(mid), ht);
    }
    return {{"''' + hdrs[0] + '''","''' + hdrs[1] + '''"},{{hc?mc2:"",ht?mt:""}}};
''')

W("query_q10a.cpp", q10("a",["%(voice)%","%(uncredited)%"],"actor","[ru]",2005,["uncredited_voiced_character","russian_movie"]))
W("query_q10b.cpp", q10("b",["%(producer)%"],"actor","[ru]",2010,["uncredited_voiced_character","russian_mov_with_actor_producer"]))
W("query_q10c.cpp", q10("c",["%(producer)%"],"actor","[us]",1990,["uncredited_voiced_character","movie_with_american_producer"]))

print("Generated Q10")

# ============================================================================
# Q11-Q33: I'll continue with all remaining queries
# These get progressively more complex but follow similar patterns
# ============================================================================

# Q11a
W("query_q11a.cpp", wrap("run_q11a","parse_q11a",'''
    int32_t ct_id = db.company_type.production_companies_id;
    auto kit = db.keyword.keyword_to_id.find("sequel");
    if (kit == db.keyword.keyword_to_id.end()) return {{"from_company","movie_link_type","non_polish_sequel_movie"},{{"","",""}}};
    std::vector<int32_t> lt_ids;
    for (const auto& [l,lid] : db.link_type.link_to_id) if (l.find("follow")!=std::string::npos) lt_ids.push_back(lid);
    auto mki = db.movie_keyword.keyword_to_movies.find(kit->second);
    if (mki == db.movie_keyword.keyword_to_movies.end()) return {{"from_company","movie_link_type","non_polish_sequel_movie"},{{"","",""}}};
    std::string mn, ml2, mt; bool hn=false, hl=false, ht=false;
    for (int32_t mid : mki->second) {
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py < 1950 || py > 2000) continue;
        uint32_t lb = db.movie_link.movie_csr.begin(mid), le = db.movie_link.movie_csr.end(mid);
        bool fl = false; std::string bl;
        for (uint32_t li = lb; li < le; ++li) {
            int32_t lt = db.movie_link.link_type_id[li];
            for (int32_t ok : lt_ids) if (lt==ok) { auto ln = db.link_type.id_to_link[lt-db.link_type.min_id]; update_min_str(bl,ln,fl); }
        }
        if (!fl) continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        bool fm = false; std::string bc; bool hbc=false;
        for (uint32_t mi = mb; mi < me; ++mi) {
            if (db.movie_companies.company_type_id[mi] != ct_id) continue;
            if (!db.movie_companies.note.is_null(mi)) continue;
            int32_t cid = db.movie_companies.company_id[mi];
            if (!db.company_name.valid_id(cid)) continue;
            if (db.company_name.get_country_code(cid) == "[pl]") continue;
            auto cn = db.company_name.get_name(cid);
            if (!like_match(cn,"%Film%") && !like_match(cn,"%Warner%")) continue;
            fm = true; update_min_str(bc,cn,hbc);
        }
        if (!fm) continue;
        update_min_str(mn,bc,hn); update_min_str(ml2,bl,hl); update_min_str(mt,db.title.get_title(mid),ht);
    }
    return {{"from_company","movie_link_type","non_polish_sequel_movie"},{{hn?mn:"",hl?ml2:"",ht?mt:""}}};
'''))

# Q11b
W("query_q11b.cpp", wrap("run_q11b","parse_q11b",'''
    int32_t ct_id = db.company_type.production_companies_id;
    auto kit = db.keyword.keyword_to_id.find("sequel");
    if (kit == db.keyword.keyword_to_id.end()) return {{"from_company","movie_link_type","sequel_movie"},{{"","",""}}};
    std::vector<int32_t> lt_ids;
    for (const auto& [l,lid] : db.link_type.link_to_id) if (l.find("follows")!=std::string::npos) lt_ids.push_back(lid);
    auto mki = db.movie_keyword.keyword_to_movies.find(kit->second);
    if (mki == db.movie_keyword.keyword_to_movies.end()) return {{"from_company","movie_link_type","sequel_movie"},{{"","",""}}};
    std::string mn, ml2, mt; bool hn=false, hl=false, ht=false;
    for (int32_t mid : mki->second) {
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py != 1998) continue;
        auto ttl = db.title.get_title(mid);
        if (!like_match(ttl,"%Money%")) continue;
        uint32_t lb = db.movie_link.movie_csr.begin(mid), le = db.movie_link.movie_csr.end(mid);
        bool fl = false; std::string bl;
        for (uint32_t li = lb; li < le; ++li) {
            int32_t lt = db.movie_link.link_type_id[li];
            for (int32_t ok : lt_ids) if (lt==ok) { update_min_str(bl,db.link_type.id_to_link[lt-db.link_type.min_id],fl); }
        }
        if (!fl) continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        bool fm = false; std::string bc; bool hbc=false;
        for (uint32_t mi = mb; mi < me; ++mi) {
            if (db.movie_companies.company_type_id[mi] != ct_id) continue;
            if (!db.movie_companies.note.is_null(mi)) continue;
            int32_t cid = db.movie_companies.company_id[mi];
            if (!db.company_name.valid_id(cid)) continue;
            if (db.company_name.get_country_code(cid) == "[pl]") continue;
            auto cn = db.company_name.get_name(cid);
            if (!like_match(cn,"%Film%") && !like_match(cn,"%Warner%")) continue;
            fm = true; update_min_str(bc,cn,hbc);
        }
        if (!fm) continue;
        update_min_str(mn,bc,hn); update_min_str(ml2,bl,hl); update_min_str(mt,ttl,ht);
    }
    return {{"from_company","movie_link_type","sequel_movie"},{{hn?mn:"",hl?ml2:"",ht?mt:""}}};
'''))

# Q11c
W("query_q11c.cpp", wrap("run_q11c","parse_q11c",'''
    std::vector<int32_t> ct_ids;
    for (const auto& [k2,cid] : db.company_type.kind_to_id) if (k2 != "production companies") ct_ids.push_back(cid);
    std::vector<std::string> kws = {"sequel","revenge","based-on-novel"};
    std::vector<int32_t> km;
    for (const auto& kw : kws) { auto ki = db.keyword.keyword_to_id.find(kw); if (ki==db.keyword.keyword_to_id.end()) continue;
        auto mi2 = db.movie_keyword.keyword_to_movies.find(ki->second); if (mi2!=db.movie_keyword.keyword_to_movies.end()) km.insert(km.end(),mi2->second.begin(),mi2->second.end()); }
    std::sort(km.begin(),km.end()); km.erase(std::unique(km.begin(),km.end()),km.end());
    std::string mn, mp, mt; bool hn=false, hp=false, ht=false;
    for (int32_t mid : km) {
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py <= 1950) continue;
        uint32_t lb = db.movie_link.movie_csr.begin(mid), le = db.movie_link.movie_csr.end(mid);
        if (lb == le) continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        for (uint32_t mi = mb; mi < me; ++mi) {
            int32_t ct = db.movie_companies.company_type_id[mi];
            bool ok = false; for (int32_t x : ct_ids) if (ct==x) { ok=true; break; } if (!ok) continue;
            if (db.movie_companies.note.is_null(mi)) continue;
            int32_t cid = db.movie_companies.company_id[mi];
            if (!db.company_name.valid_id(cid)) continue;
            if (db.company_name.get_country_code(cid) == "[pl]") continue;
            auto cn = db.company_name.get_name(cid);
            if (!like_match(cn,"20th Century Fox%") && !like_match(cn,"Twentieth Century Fox%")) continue;
            update_min_str(mn,cn,hn); update_min_str(mp,db.movie_companies.note.get(mi),hp);
            update_min_str(mt,db.title.get_title(mid),ht);
        }
    }
    return {{"from_company","production_note","movie_based_on_book"},{{hn?mn:"",hp?mp:"",ht?mt:""}}};
'''))

# Q11d - same as Q11c but without cn.name filter
W("query_q11d.cpp", wrap("run_q11d","parse_q11d",'''
    std::vector<int32_t> ct_ids;
    for (const auto& [k2,cid] : db.company_type.kind_to_id) if (k2 != "production companies") ct_ids.push_back(cid);
    std::vector<std::string> kws = {"sequel","revenge","based-on-novel"};
    std::vector<int32_t> km;
    for (const auto& kw : kws) { auto ki = db.keyword.keyword_to_id.find(kw); if (ki==db.keyword.keyword_to_id.end()) continue;
        auto mi2 = db.movie_keyword.keyword_to_movies.find(ki->second); if (mi2!=db.movie_keyword.keyword_to_movies.end()) km.insert(km.end(),mi2->second.begin(),mi2->second.end()); }
    std::sort(km.begin(),km.end()); km.erase(std::unique(km.begin(),km.end()),km.end());
    std::string mn, mp, mt; bool hn=false, hp=false, ht=false;
    for (int32_t mid : km) {
        if (!db.title.valid_id(mid)) continue;
        int32_t py = db.title.get_production_year(mid);
        if (py == NULL_SENTINEL || py <= 1950) continue;
        uint32_t lb = db.movie_link.movie_csr.begin(mid), le = db.movie_link.movie_csr.end(mid);
        if (lb == le) continue;
        uint32_t mb = db.movie_companies.movie_csr.begin(mid), me = db.movie_companies.movie_csr.end(mid);
        for (uint32_t mi = mb; mi < me; ++mi) {
            int32_t ct = db.movie_companies.company_type_id[mi];
            bool ok = false; for (int32_t x : ct_ids) if (ct==x) { ok=true; break; } if (!ok) continue;
            if (db.movie_companies.note.is_null(mi)) continue;
            int32_t cid = db.movie_companies.company_id[mi];
            if (!db.company_name.valid_id(cid)) continue;
            if (db.company_name.get_country_code(cid) == "[pl]") continue;
            update_min_str(mn,db.company_name.get_name(cid),hn); update_min_str(mp,db.movie_companies.note.get(mi),hp);
            update_min_str(mt,db.title.get_title(mid),ht);
        }
    }
    return {{"from_company","production_note","movie_based_on_book"},{{hn?mn:"",hp?mp:"",ht?mt:""}}};
'''))

print("Generated Q11")

# For the remaining Q12-Q33, I realize this approach is reaching its limits in a single file.
# Let me split the generation into a Part 2 file.
print("Part 1 done: Q3-Q11")
