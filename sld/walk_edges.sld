<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor version="1.0.0"
    xmlns="http://www.opengis.net/sld"
    xmlns:ogc="http://www.opengis.net/ogc"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://www.opengis.net/sld http://schemas.opengis.net/sld/1.0.0/StyledLayerDescriptor.xsd">
  <NamedLayer>
    <Name>walk_edges</Name>
    <UserStyle>
      <Name>walk_edges</Name>
      <Title>Walkable street network</Title>
      <Abstract>OSMnx walk network; foot-only ways dashed; hidden at small scales</Abstract>
      <FeatureTypeStyle>
        <Rule>
          <Name>Foot-only ways</Name>
          <Title>Footway / path / pedestrian / steps</Title>
          <ogc:Filter><ogc:Or><ogc:PropertyIsLike wildCard="*" singleChar="." escape="!"><ogc:PropertyName>highway</ogc:PropertyName><ogc:Literal>*footway*</ogc:Literal></ogc:PropertyIsLike><ogc:PropertyIsLike wildCard="*" singleChar="." escape="!"><ogc:PropertyName>highway</ogc:PropertyName><ogc:Literal>*path*</ogc:Literal></ogc:PropertyIsLike><ogc:PropertyIsLike wildCard="*" singleChar="." escape="!"><ogc:PropertyName>highway</ogc:PropertyName><ogc:Literal>*pedestrian*</ogc:Literal></ogc:PropertyIsLike><ogc:PropertyIsLike wildCard="*" singleChar="." escape="!"><ogc:PropertyName>highway</ogc:PropertyName><ogc:Literal>*steps*</ogc:Literal></ogc:PropertyIsLike></ogc:Or></ogc:Filter>
          <MaxScaleDenominator>40000</MaxScaleDenominator>
          <LineSymbolizer>
            <Stroke>
              <CssParameter name="stroke">#92400e</CssParameter>
              <CssParameter name="stroke-width">1.2</CssParameter>
              <CssParameter name="stroke-dasharray">4 3</CssParameter>
            </Stroke>
          </LineSymbolizer>
        </Rule>
        <Rule>
          <Name>Streets</Name>
          <Title>Streets usable on foot</Title>
          <ElseFilter/>
          <MaxScaleDenominator>40000</MaxScaleDenominator>
          <LineSymbolizer>
            <Stroke>
              <CssParameter name="stroke">#6b7280</CssParameter>
              <CssParameter name="stroke-width">0.8</CssParameter>
              <CssParameter name="stroke-opacity">0.8</CssParameter>
            </Stroke>
          </LineSymbolizer>
        </Rule>
      </FeatureTypeStyle>
    </UserStyle>
  </NamedLayer>
</StyledLayerDescriptor>
