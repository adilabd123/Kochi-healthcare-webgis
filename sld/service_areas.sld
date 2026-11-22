<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>service_areas</Name>
    <UserStyle>
      <Name>service_areas</Name>
      <Title>Walking service areas (5/10/15 min)</Title>
      <Abstract>Filter with CQL_FILTER, e.g. facility_set='all' AND speed_kmh=4.8</Abstract>
      <FeatureTypeStyle>
        <Rule>
          <Name>15 min walk</Name>
          <Title>15 min walk</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>minutes</ogc:PropertyName><ogc:Literal>15</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#fde68a</CssParameter><CssParameter name="fill-opacity">0.35</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#d97706</CssParameter><CssParameter name="stroke-width">1.2</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
      </FeatureTypeStyle>
      <FeatureTypeStyle>
        <Rule>
          <Name>10 min walk</Name>
          <Title>10 min walk</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>minutes</ogc:PropertyName><ogc:Literal>10</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#fdba74</CssParameter><CssParameter name="fill-opacity">0.35</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#ea580c</CssParameter><CssParameter name="stroke-width">1.2</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
      </FeatureTypeStyle>
      <FeatureTypeStyle>
        <Rule>
          <Name>5 min walk</Name>
          <Title>5 min walk</Title>
          <ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>minutes</ogc:PropertyName><ogc:Literal>5</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
          <PolygonSymbolizer>
            <Fill><CssParameter name="fill">#f87171</CssParameter><CssParameter name="fill-opacity">0.35</CssParameter></Fill>
            <Stroke><CssParameter name="stroke">#b91c1c</CssParameter><CssParameter name="stroke-width">1.2</CssParameter></Stroke>
          </PolygonSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
