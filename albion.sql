-- public schema for database interface
create schema albion
;

create function albion.snap_distance()
returns real
language plpgsql immutable
as
$$
    begin
        return (select snap_distance from _albion.metadata);
    end;
$$
;


create or replace function albion.update_hole_geom()
returns boolean
language plpgsql
as
$$
    begin
        with dz as (
            select 
                hole_id, 
                from_ as md2, coalesce(lag(from_) over w, 0) as md1,
                (deep + 90)*pi()/180 as wd2,  coalesce(lag((deep+90)*pi()/180) over w, 0) as wd1,
                azimuth*pi()/180 as haz2,  coalesce(lag(azimuth*pi()/180) over w, 0) as haz1
            from _albion.deviation 
            where azimuth >= 0 and azimuth <=360 and deep < 0 and deep > -180
            window w AS (partition by hole_id order by from_)
        ),
        pt as (
            select dz.hole_id, md2, wd2, haz2, 
            st_x(c.geom) + sum(0.5 * (md2 - md1) * (sin(wd1) * sin(haz1) + sin(wd2) * sin(haz2))) over w as x,
            st_y(c.geom) + sum(0.5 * (md2 - md1) * (sin(wd1) * cos(haz1) + sin(wd2) * cos(haz2))) over w as y,
            st_z(c.geom) - sum(0.5 * (md2 - md1) * (cos(wd2) + cos(wd1))) over w as z
            from dz join _albion.hole as h on h.id=hole_id join _albion.collar as c on c.id=h.collar_id
            window w AS (partition by hole_id order by md1)
        ),
        line as (
            select hole_id, st_makeline(('SRID=32632; POINTZ('||x||' '||y||' '||z||')')::geometry order by md2 asc) as geom
            from pt
            group by hole_id
        )
        update _albion.hole as h set geom=(select st_addpoint(geom, (
                select c.geom from _albion.hole as hh join _albion.collar as c on c.id=hh.collar_id
                where hh.id=h.id), 0)
            from line as l where l.hole_id=h.id);
        return 't'::boolean;
    end;
$$
;


create materialized view albion.cell
as
with collec as (
    select
            (st_dump(
                coalesce(
                    st_split(
                        a.geom,
                        (select st_collect(geom)
                            from _albion.grid as b
                            where a.id!=b.id and st_intersects(a.geom, b.geom)
                            and st_dimension(st_intersection(a.geom, b.geom))=0)),
                    a.geom)
        )).geom as geom
    from _albion.grid as a
),
poly as (
    select (st_dump(st_polygonize(geom))).geom as geom from collec
)
select row_number() over() as id, geom::geometry('POLYGON', {srid}) from poly where geom is not null
;

create function albion.fix_grid_topology()
returns boolean
language plpgsql
as
$$
    begin
        with
        collar as (
            select st_force2D(geom) as geom from _albion.collar
        ),
        inter as (
            select st_intersection(a.geom, b.geom) as geom
            from _albion.grid as a, _albion.grid as b
            where st_intersects(a.geom, b.geom)
            and st_dimension(st_intersection(a.geom, b.geom))=0
        ),
        other as (
            select geom from collar
            union
            select geom from inter
            where not exists (select 1 from collar where st_dwithin(collar.geom, inter.geom, albion.snap_distance()))
        ),
        ends as (
            select id, st_startpoint(g.geom) as geom from _albion.grid as g
            where not exists (select 1 from other where st_dwithin(other.geom, st_startpoint(g.geom), albion.snap_distance()))
            union
            select id, st_endpoint(geom) as geom from _albion.grid as g
            where not exists (select 1 from other where st_dwithin(other.geom, st_endpoint(g.geom), albion.snap_distance()))
        ),
        snap as (
            select st_collect(geom) as geom
            from (select geom from other union select geom from ends) as t
        )
        update _albion.grid set geom=coalesce(st_snap(geom, (select geom from snap), albion.snap_distance()), geom)
        where (select geom from snap) is not null;

        return 't'::boolean;
    end;
$$
;

create view albion.grid as
select id, geom, st_azimuth(st_startpoint(geom), st_endpoint(geom)) as azimuth
from _albion.grid
;

create function albion.grid_instead_fct()
returns trigger
language plpgsql
as
$$
    begin
        -- snap geom to collars (adds points to geom)
        if tg_op = 'INSERT' or tg_op = 'UPDATE' then
            select st_removerepeatedpoints(new.geom, albion.snap_distance()) into new.geom;

            with snap as (
                select st_collect(geom) as geom
                from (
                    select st_force2D(geom) as geom
                    from  _albion.collar
                    where st_dwithin(geom, new.geom, albion.snap_distance())
                    union all
                    select st_closestpoint(geom, new.geom) as geom
                    from _albion.grid as g
                    where st_dwithin(geom, new.geom, albion.snap_distance())
                    and st_distance(st_closestpoint(g.geom, new.geom), (select c.geom from _albion.collar as c order by c.geom <-> st_closestpoint(g.geom, new.geom) limit 1)) > albion.snap_distance()
                ) as t
            )
            select coalesce(st_snap(new.geom, (select geom from snap), albion.snap_distance()), new.geom) into new.geom; 

            with new_points as (
                select st_collect(geom) as geom from (select (st_dumppoints(new.geom)).geom as geom) as t
            ),
            nearby as (
                select id from _albion.grid
                where st_dwithin(geom, new.geom, albion.snap_distance())
            )
            update _albion.grid as g set geom = st_snap(g.geom, (select geom from new_points), albion.snap_distance())
            where id in (select id from nearby);
        end if;

        if tg_op = 'INSERT' then
            insert into _albion.grid(geom) values(new.geom) returning id into new.id;
        elsif tg_op = 'UPDATE' then
            update _albion.grid set geom=new.geom where id=new.id;
        elsif tg_op = 'DELETE' then
            delete from _albion.grid where id=old.id;
        end if;

        --perform albion.fix_grid_topology();
        --refresh materialized view albion.cell;

        if tg_op = 'INSERT' or tg_op = 'UPDATE' then
            return new;
        elsif tg_op = 'DELETE' then
            return old;
        end if;
    end;
$$
;

create trigger grid_instead_trig
    instead of insert or update or delete on albion.grid
       for each row execute procedure albion.grid_instead_fct()
;

create view albion.collar as select id, geom, comments from _albion.collar
;

create view albion.close_point as
with ends as (
    select id, st_startpoint(geom) as geom from _albion.grid
    union
    select id, st_endpoint(geom) as geom from _albion.grid
)
select row_number() over() as id, e.geom::geometry('POINT', {srid}) 
from ends as e
where exists (
    select 1 
    from albion.grid as g 
    where st_dwithin(e.geom, g.geom, 2*(select snap_distance from _albion.metadata)) 
    and not st_intersects(e.geom, g.geom))
;

create materialized view albion.small_edge as
with all_points as (
    select distinct (st_dumppoints(geom)).geom as geom from albion.grid
)
select row_number() over() as id, a.geom::geometry('POINT', {srid}) 
from all_points as a, all_points as b
where st_dwithin(a.geom, b.geom, 2*albion.snap_distance())
and not st_intersects(a.geom, b.geom)
;

create view albion.metadata as select id, srid, current_section, snap_distance, origin, precision from _albion.metadata
;

create view albion.hole as select id, collar_id, geom from _albion.hole
;

create view albion.deviation as select hole_id, from_, deep, azimuth from _albion.deviation
;

create view albion.formation as select hole_id, from_, to_, code, comments from _albion.formation
;

create view albion.resistivity as select hole_id, from_, to_, rho from _albion.resistivity
;

create view albion.radiometry as select hole_id, from_, to_, gamma from _albion.radiometry
;

create view albion.lithology as select hole_id, from_, to_, code, comments from _albion.lithology
;

create view albion.mineralization as select hole_id, from_, to_, oc, accu, grade from _albion.mineralization
;







/*
create materialized view albion.grid_vertices
as
with
collar as (
    select st_collect(st_force2D(geom)) as geom from _albion.collar
),
inter as (
    select st_collect(st_snap(st_intersection(a.geom, b.geom), (select geom from collar), albion.snap_distance())) as geom
    from _albion.grid as a, _albion.grid as b
    where a.geom && b.geom
    and st_dimension(st_intersection(a.geom, b.geom))=0
),
other as (
    select st_union(geom) as geom from (select geom from collar union all select geom from inter) as t
),
ends as (
    select st_collect(geom) as geom
    from (
        select st_startpoint(geom) as geom from _albion.grid
        union all
        select st_endpoint(geom) as geom from _albion.grid
    ) as t
),
snap as (
    select (st_dumppoints(st_union(geom))).geom as geom
    from (select geom from other union all select geom from ends) as t
)
select row_number() over() as id, geom from snap
;

update _albion.grid as g set geom=st_snap(g.geom, st_force2D(c.geom), albion.snap_distance())
from _albion.collar as c
where st_dwithin(c.geom, g.geom, albion.snap_distance())
;


create function albion.grid_after_fct()
returns trigger
language plpgsql
as
$$
    begin
        select albion.fix_grid_topology();
        refresh materialized view albion.cell;
        if tg_op = 'INSERT' or tg_op = 'UPDATE' then
            return new;
        elsif tg_op = 'DELETE' then
            return old;
        end if;
    end;
$$
;

create trigger grid_after_trig
    after insert or update or delete on _albion.grid
       for each statement execute procedure albion.grid_after_fct()
;
*/


